from logging import Logger
from typing import List, Optional

from model.models import PluginContext
from usecase import PluginUseCase
from util import LogUtil


class PluginEngine:
    _logger: Logger
    _plugins: List[str] = list()

    def __init__(self, common_plugins: Optional[List] = None) -> None:
        self._logger = LogUtil.create()
        self.use_case = PluginUseCase()
        if common_plugins is None:
            self._plugins = []
        else:
            self._plugins += common_plugins
        self._context = PluginContext()

    def get_context(self) -> PluginContext:
        return self._context

    def invoke(self) -> None:
        self._logger.debug('------------------------install plugins----------------------------')
        self.__install_plugins()
        self._logger.debug('------------------------invoke plugins----------------------------')
        self.__invoke_on_plugins()

    def attach_plugins(self, plugins: list) -> 'PluginEngine':
        self._plugins += plugins
        return self

    def __install_plugins(self) -> None:
        self.use_case.discover_plugins(self._plugins)

    def __invoke_on_plugins(self):
        """Apply all of the plugins on the argument supplied to this function
        """
        for module in self.use_case.modules:
            plugin = self.use_case.register_plugin(module.type, self._logger, module.meta)
            res = self.use_case.hook_plugin(plugin)(self._context)
            if res.is_succeed():
                self._context.set_plugin_data(module.meta.name, res.data)
                self._logger.info(f'{module.meta.name} invoke succeed')
            else:
                self._logger.error(f'{module.meta.name} invoke failed, error in {res.errorMsg}')


class PluginEngineFactory:

    @staticmethod
    def create_html2markdown_engine() -> PluginEngine:
        return PluginEngine(common_plugins=['http-request', 'html2markdown'])
