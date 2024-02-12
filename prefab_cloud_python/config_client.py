from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import sseclient
import base64
import prefab_pb2 as Prefab
import os

from ._count_down_latch import CountDownLatch
from .config_loader import ConfigLoader
from .config_resolver import ConfigResolver
from .config_value_unwrapper import ConfigValueUnwrapper
from .context import Context
from .config_resolver import Evaluation
from .constants import NoDefaultProvided

from google.protobuf.json_format import MessageToJson, Parse


STALE_CACHE_WARN_HOURS = 5

logger = logging.getLogger(__name__)


class InitializationTimeoutException(Exception):
    def __init__(self, timeout_seconds, key):
        super().__init__(
            f"Prfeab couldn't initialize in {timeout_seconds} second timeout. Trying to fetch key `{key}`."
        )


class MissingDefaultException(Exception):
    def __init__(self, key):
        super().__init__(
            f"""No value found for key '{key}' and no default was provided.

If you'd prefer returning `None` rather than raising when this occurs, modify the `on_no_default` value you provide in your Options."""
        )


class ConfigClient:
    def __init__(self, base_client):
        self.is_initialized = threading.Event()
        self.checkpointing_thread = None
        self.streaming_thread = None
        self.sse_client = None
        logger.info("Initializing ConfigClient")
        self.base_client = base_client
        self.options = base_client.options
        self.init_latch = CountDownLatch()
        self.checkpoint_freq_secs = 60
        self.config_loader = ConfigLoader(base_client)
        self.config_resolver = ConfigResolver(base_client, self.config_loader)
        self._cache_path = None
        self.set_cache_path()

        if self.options.is_local_only():
            self.finish_init("local only")
        elif self.options.has_datafile():
            self.load_json_file(self.options.datafile)
        else:
            # don't load checkpoint here, that'll block the caller. let the thread do it
            self.start_checkpointing_thread()
            self.start_streaming()

    def get(
        self,
        key,
        default=NoDefaultProvided,
        context: Optional[dict | Context] = Context.get_current(),
    ):
        evaluation_result = self.__get(key, None, {}, context=context)
        if evaluation_result is not None:
            self.base_client.telemetry_manager.record_evaluation(evaluation_result)
            if evaluation_result.config:
                return evaluation_result.unwrapped_value()
        return self.handle_default(key, default)

    def __get(
        self,
        key,
        lookup_key,
        properties,
        context: Optional[dict | Context] = Context.get_current(),
    ) -> None | Evaluation:
        ok_to_proceed = self.init_latch.wait(
            timeout=self.options.connection_timeout_seconds
        )
        if not ok_to_proceed:
            if self.options.on_connection_failure == "RAISE":
                raise InitializationTimeoutException(
                    self.options.connection_timeout_seconds, key
                )
            logger.warning(
                f"Couldn't initialize in {self.options.connection_timeout_seconds}. Key {key}. Returning what we have.",
            )
        return self.config_resolver.get(key, context=context)

    def handle_default(self, key, default):
        if default != NoDefaultProvided:
            return default
        if self.options.on_no_default == "RAISE":
            raise MissingDefaultException(key)
        return None

    def load_checkpoint(self):
        if self.load_checkpoint_from_api_cdn():
            return
        if self.load_cache():
            return
        logger.warning("No success loading checkpoints")

    def start_checkpointing_thread(self):
        self.checkpointing_thread = threading.Thread(
            target=self.checkpointing_loop, daemon=True
        )
        self.checkpointing_thread.start()

    def start_streaming(self):
        self.streaming_thread = threading.Thread(
            target=self.streaming_loop, daemon=True
        )
        self.streaming_thread.start()

    def streaming_loop(self):
        while not self.base_client.shutdown_flag.is_set():
            try:
                url = "%s/api/v1/sse/config" % self.options.prefab_api_url
                headers = {
                    "x-prefab-start-at-id": f"{self.config_loader.highwater_mark}",
                }
                response = self.base_client.session.get(
                    url,
                    headers=headers,
                    stream=True,
                    auth=("authuser", self.options.api_key),
                    timeout=None,
                )

                self.sse_client = sseclient.SSEClient(response)

                for event in self.sse_client.events():
                    if event.data:
                        logger.info("Loading data from SSE stream")
                        configs = Prefab.Configs.FromString(
                            base64.b64decode(event.data)
                        )
                        self.load_configs(configs, "sse_streaming")
            except Exception as e:
                if not self.base_client.shutdown_flag.is_set:
                    logger.info(f"Issue with streaming connection, will restart {e}")

    def checkpointing_loop(self):
        while not self.base_client.shutdown_flag.is_set():
            try:
                self.load_checkpoint()
                time.sleep(self.checkpoint_freq_secs)
            except Exception as e:
                logger.warn(f"Issue checkpointing, will restart {e}")

    def load_checkpoint_from_api_cdn(self):
        url = "%s/api/v1/configs/0" % self.options.url_for_api_cdn
        logger.warning(f"Loading config from {url}")
        response = self.base_client.session.get(
            url, auth=("authuser", self.options.api_key)
        )
        if response.ok:
            configs = Prefab.Configs.FromString(response.content)
            self.load_configs(configs, "remote_api_cdn")
            return True
        else:
            logger.info(
                "Checkpoint remote_cdn_api failed to load",
            )
            return False

    def load_configs(self, configs, source):
        project_id = configs.config_service_pointer.project_id
        project_env_id = configs.config_service_pointer.project_env_id
        self.config_resolver.project_env_id = project_env_id
        starting_highwater_mark = self.config_loader.highwater_mark

        default_contexts = {}
        if configs.default_context and configs.default_context.contexts is not None:
            for context in configs.default_context.contexts:
                values = {}
                for k, v in context.values.items():
                    values[k] = ConfigValueUnwrapper(v, self.config_resolver).unwrap()
                default_contexts[context.type] = values

        self.config_resolver.default_context = default_contexts

        for config in configs.configs:
            self.config_loader.set(config, source)
        if self.config_loader.highwater_mark > starting_highwater_mark:
            logger.info(
                f"Found new checkpoint with highwater id {self.config_loader.highwater_mark} from {source} in project {project_id} environment: {project_env_id} and namespace {self.base_client.options.namespace}",
            )
        else:
            logger.debug(
                f"Checkpoint with highwater id {self.config_loader.highwater_mark} from {source}. No changes.",
            )
        self.config_resolver.update()
        self.finish_init(source)

    def cache_configs(self, configs):
        if not self.options.use_local_cache:
            return
        if not self.cache_path:
            return
        with open(self.cache_path, "w") as f:
            f.write(MessageToJson(configs))
            logger.debug(f"Cached configs to {self.cache_path}")

    def load_cache(self):
        if not self.options.use_local_cache:
            return False
        if not self.cache_path:
            return False
        try:
            with open(self.cache_path, "r") as f:
                configs = Parse(f.read(), Prefab.Configs())
                self.load_configs(configs, "cache")

                hours_old = round(
                    (time.mktime(time.localtime()) - os.path.getmtime(self.cache_path))
                    / 3600,
                    2,
                )
                if hours_old > STALE_CACHE_WARN_HOURS:
                    logger.info(f"Stale Cache Load: {hours_old} hours old")
                return True
        except OSError as e:
            logger.info("error loading from cache", e)
            return False

    def load_json_file(self, datafile):
        with open(datafile) as f:
            configs = Parse(f.read(), Prefab.Configs())
            self.load_configs(configs, "datafile")

    def finish_init(self, source):
        self.is_initialized.set()
        self.init_latch.count_down()
        logger.info(f"Unlocked config via {source}")

    def set_cache_path(self):
        home_dir_cache_path = None
        home_dir = os.environ.get("HOME")
        if home_dir:
            home_dir_cache_path = os.path.join(home_dir, ".cache")
        cache_path = os.environ.get("XDG_CACHE_HOME", home_dir_cache_path)
        if cache_path:
            file_name = f"prefab.cache.{self.base_client.options.api_key_id}.json"
            self.cache_path = os.path.join(cache_path, file_name)

    @property
    def cache_path(self):
        if self._cache_path:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
        return self._cache_path

    @cache_path.setter
    def cache_path(self, path):
        self._cache_path = path

    def record_log(self, path, severity):
        self.base_client.record_log(path, severity)

    def is_ready(self) -> bool:
        return self.is_initialized.is_set()

    def close(self) -> None:
        if self.sse_client:
            self.sse_client.close()
