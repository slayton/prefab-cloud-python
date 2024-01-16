import functools
from .context import Context, ScopedContext
from .config_client import ConfigClient
from .feature_flag_client import FeatureFlagClient
from .context_shape_aggregator import ContextShapeAggregator
from .log_path_aggregator import LogPathAggregator
from .logger_client import LoggerClient
from .logger_filter import LoggerFilter
from .options import Options
from typing import Optional
import base64
import prefab_pb2 as Prefab
import uuid
import requests
from urllib.parse import urljoin


ConfigValueType = Optional[int | float | bool | str | list[str]]
PostBodyType = Prefab.Loggers | Prefab.ContextShapes


class Client:
    max_sleep_sec = 10
    base_sleep_sec = 0.5
    no_default_provided = "NO_DEFAULT_PROVIDED"

    def __init__(self, options: Options) -> None:
        self.options = options
        self.instance_hash = str(uuid.uuid4())
        self.log_path_aggregator = LogPathAggregator(
            self, self.options.collect_max_paths, self.options.collect_sync_interval
        )
        self.logger = LoggerClient(
            self.options.log_prefix, self.options.log_boundary, self.log_path_aggregator
        )
        self.log_path_aggregator.client = self

        self.context_shape_aggregator = ContextShapeAggregator(
            self, self.options.collect_max_shapes, self.options.collect_sync_interval
        )

        if not options.is_local_only():
            self.log_path_aggregator.start_periodic_sync()
            self.context_shape_aggregator.start_periodic_sync()

        self.namespace = options.namespace
        self.api_url = options.prefab_api_url
        self.grpc_url = options.prefab_grpc_url
        self.session = requests.Session()
        if options.is_local_only():
            self.logger.log_internal("info", "Prefab running in local-only mode")
        else:
            self.logger.log_internal(
                "info",
                "Prefab connecting to %s and %s, secure %s"
                % (
                    options.prefab_api_url,
                    options.prefab_grpc_url,
                    options.http_secure,
                ),
            )

        self.context().clear()
        self.config_client()

    def get(
        self,
        key: str,
        default: ConfigValueType = "NO_DEFAULT_PROVIDED",
        context: str | Context = "NO_CONTEXT_PROVIDED",
    ) -> ConfigValueType:
        if self.is_ff(key):
            if default == "NO_DEFAULT_PROVIDED":
                default = None
            return self.feature_flag_client().get(
                key, default=default, context=self.resolve_context_argument(context)
            )
        else:
            return self.config_client().get(
                key, default=default, context=self.resolve_context_argument(context)
            )

    def enabled(
        self, feature_name: str, context: str | Context = "NO_CONTEXT_PROVIDED"
    ) -> bool:
        return self.feature_flag_client().feature_is_on_for(
            feature_name, context=self.resolve_context_argument(context)
        )

    def is_ff(self, key: str) -> bool:
        raw = self.config_client().config_resolver.raw(key)
        if raw is not None and raw.config_type == Prefab.ConfigType.Value(
            "FEATURE_FLAG"
        ):
            return True
        return False

    def resolve_context_argument(self, context: str | Context) -> Context:
        if context != "NO_CONTEXT_PROVIDED":
            return context
        return Context.get_current()

    def context(self) -> Context:
        return Context.get_current()

    def scoped_context(context: Context) -> ScopedContext:
        return Context.scope(context)

    @functools.cache
    def config_client(self) -> ConfigClient:
        client = ConfigClient(self, timeout=5.0)
        return client

    @functools.cache
    def feature_flag_client(self) -> FeatureFlagClient:
        return FeatureFlagClient(self)

    def post(self, path: str, body: PostBodyType) -> requests.models.Response:
        headers = {
            "Content-Type": "application/x-protobuf",
            "Accept": "application/x-protobuf",
        }

        endpoint = urljoin(self.options.prefab_api_url or "", path)

        return self.session.post(
            endpoint,
            headers=headers,
            data=body.SerializeToString(),
            auth=("authuser", self.options.api_key or ""),

    def logging_filter(self):
        return LoggerFilter(
            self.config_client(),
            prefix=self.options.log_prefix,
            log_boundary=self.options.log_boundary
        )
