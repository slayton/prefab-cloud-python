from __future__ import annotations
import functools

from .read_write_lock import ReadWriteLock
from .config_value_unwrapper import ConfigValueUnwrapper
from .context import Context
import prefab_pb2 as Prefab
import google


class ConfigResolver:
    def __init__(self, base_client, config_loader):
        self.lock = ReadWriteLock()
        self.base_client = base_client
        self.config_loader = config_loader
        self.project_env_id = 0
        self.default_context = {}
        self.make_local()

    def get(self, key, context=Context.get_current()) -> "Evaluation | None":
        self.lock.acquire_read()
        raw_config = self.raw(key)
        self.lock.release_read()

        if raw_config is None:
            return None
        return self.evaluate(raw_config, context=context)

    def raw(self, key) -> Prefab.ConfigValue | None:
        via_key = self.local_store.get(key)
        if via_key is not None:
            return via_key["config"]
        return None

    def evaluate(self, config, context=Context.get_current()) -> "Evaluation | None":
        return CriteriaEvaluator(
            config,
            project_env_id=self.project_env_id,
            resolver=self,
            base_client=self.base_client,
        ).evaluate(self.evaluation_context(context))

    def evaluation_context(self, context):
        if not isinstance(context, Context):
            context = Context.merge_with_current(context)
        return context.merge_default(self.default_context)

    def update(self):
        self.make_local()

    def make_local(self):
        self.lock.acquire_write()
        self.local_store = self.config_loader.calc_config()
        self.lock.release_write()

    @property
    def default_context(self):
        return self._default_context

    @default_context.setter
    def default_context(self, value):
        self._default_context = value


OPS = Prefab.Criterion.CriterionOperator


class CriteriaEvaluator:
    def __init__(self, config, project_env_id, resolver, base_client):
        self.config = config
        self.project_env_id = project_env_id
        self.resolver = resolver
        self.base_client = base_client

    def evaluate(self, props):
        for value_index, conditional_value in enumerate(
            self.matching_environment_row_values()
        ):
            if self.all_criteria_match(conditional_value, props):
                return Evaluation(
                    self.config,
                    conditional_value.value,
                    value_index,
                    0,
                    props,
                    self.resolver,
                )
        for value_index, conditional_value in enumerate(self.default_row_values()):
            if self.all_criteria_match(conditional_value, props):
                return Evaluation(
                    self.config,
                    conditional_value.value,
                    value_index,
                    1,
                    props,
                    self.resolver,
                )
        return None

    def all_criteria_match(self, conditional_value, props):
        for criterion in conditional_value.criteria:
            if not self.evaluate_criterion(criterion, props):
                return False
        return True

    def evaluate_criterion(self, criterion, properties):
        if criterion.property_name == "NAMESPACE":
            value_from_properties = self.base_client.options.namespace
        else:
            value_from_properties = properties.get(criterion.property_name)

        if criterion.operator in [OPS.LOOKUP_KEY_IN, OPS.PROP_IS_ONE_OF]:
            return self.matches(criterion, value_from_properties, properties)
        if criterion.operator in [OPS.LOOKUP_KEY_NOT_IN, OPS.PROP_IS_NOT_ONE_OF]:
            return not self.matches(criterion, value_from_properties, properties)
        if criterion.operator == OPS.IN_SEG:
            return self.in_segment(criterion, properties)
        if criterion.operator == OPS.NOT_IN_SEG:
            return not self.in_segment(criterion, properties)
        if criterion.operator == OPS.PROP_ENDS_WITH_ONE_OF:
            if value_from_properties is None:
                return False
            return any(
                [
                    str(value_from_properties).endswith(ending)
                    for ending in criterion.value_to_match.string_list.values
                ]
            )
        if criterion.operator == OPS.PROP_DOES_NOT_END_WITH_ONE_OF:
            if value_from_properties is None:
                return True
            return not any(
                [
                    str(value_from_properties).endswith(ending)
                    for ending in criterion.value_to_match.string_list.values
                ]
            )
        if criterion.operator == OPS.HIERARCHICAL_MATCH:
            return value_from_properties.startswith(criterion.value_to_match.string)
        if criterion.operator == OPS.ALWAYS_TRUE:
            return True

        self.base_client.logger.log_internal(
            "info", f"Unknown criterion operator {criterion.operator}"
        )
        return False

    def matches(self, criterion, value, properties):
        criterion_value_or_values = ConfigValueUnwrapper.deepest_value(
            criterion.value_to_match, self.config.key, properties
        ).unwrap()
        if isinstance(
            criterion_value_or_values, google._upb._message.RepeatedScalarContainer
        ):
            return str(value) in criterion_value_or_values
        return value == criterion_value_or_values

    def in_segment(self, criterion, properties):
        return (
            self.resolver.get(criterion.value_to_match.string, context=properties)
            .raw_config_value()
            .bool
        )

    def matching_environment_row_values(self):
        env_rows = [
            row for row in self.config.rows if row.project_env_id == self.project_env_id
        ]
        if env_rows == []:
            return []
        else:
            return env_rows[0].values

    def default_row_values(self):
        env_rows = [
            row for row in self.config.rows if row.project_env_id != self.project_env_id
        ]
        if env_rows == []:
            return []
        else:
            return env_rows[0].values


class Evaluation:
    def __init__(
        self,
        config: Prefab.Config,
        value: Prefab.ConfigValue,
        value_index: int,
        config_row_index: int,
        context: Context,
        resolver: ConfigResolver,
    ):
        self.config = config
        self.value = value
        self.value_index = value_index
        self.config_row_index = config_row_index
        self.context = context
        self.resolver = resolver

    def unwrapped_value(self):
        return self.deepest_value().unwrap()

    def raw_config_value(self):
        return self.value

    @functools.cache
    def deepest_value(self):
        return ConfigValueUnwrapper.deepest_value(
            self.value, self.config, self.resolver, self.context
        )
