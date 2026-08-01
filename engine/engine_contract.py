from logging import Logger
from typing import Optional, List

from model import Meta, Pair
from model.models import PluginInvokeResponse, PluginContext


class IPluginRegistry(type):
    plugin_registries: List[Pair] = list()

    def __init__(cls, name, bases, attrs):
        super().__init__(cls)
        if name != 'PluginCore':
            IPluginRegistry.plugin_registries.append(Pair(type=cls, meta=Meta(name)))


class PluginCore(object, metaclass=IPluginRegistry):
    """
    Plugin core class
    """

    meta: Meta

    def __init__(self, logger: Logger, meta: Meta) -> None:
        """
        Entry init block for plugins
        :param logger: logger that plugins can make use of
        """
        self._logger = logger
        self.meta = meta

    def invoke(self, context: PluginContext) -> PluginInvokeResponse:
        self._logger.debug(f'invoke in 【{self.meta.name}】')
        pass

    def get_logger(self):
        return self._logger
