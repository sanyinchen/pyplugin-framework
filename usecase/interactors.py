import os
from importlib import import_module
from logging import Logger
from typing import List, Any, Dict

from model import Pair, Meta
from engine import IPluginRegistry, PluginCore
from util import LogUtil
from .utilities import PluginUtility


class PluginUseCase:
    _logger: Logger
    modules: List[Pair]

    def __init__(self) -> None:
        self._logger = LogUtil.create()
        self.plugin_util = PluginUtility(self._logger)
        self.modules = list()

    def __check_loaded_plugin_state(self, plugin_module: Any):
        if len(IPluginRegistry.plugin_registries) > 0:
            latest_module = IPluginRegistry.plugin_registries[-1]
            latest_module_name = latest_module.type.__module__
            current_module_name = plugin_module.__name__
            if current_module_name == latest_module_name:
                self._logger.debug(f'Successfully imported module `{current_module_name}`')
                self.modules.append(latest_module)
            else:
                self._logger.error(
                    f'Expected to import -> `{current_module_name}` but got -> `{latest_module_name}`'
                )
            # clear plugins from the registry when we're done with them
            IPluginRegistry.plugin_registries.clear()
        else:
            self._logger.error(f'No plugin found in registry for module: {plugin_module}')

    def __search_for_plugins_in(self, plugins_path: List[str]):
        for directory in plugins_path:
            base_plugin_package = self.plugin_util.get_root_plugin_package()
            plugin_directory = self.plugin_util.get_root_plugin_dir()
            # 插件既可以直接挂在 plugins 下（http-request），也可以带业务分组（common/xxx-plugin）
            directory = directory.strip('/')
            index = directory.rfind('/')
            if index < 0:
                last_package = directory
                package_name = base_plugin_package
            else:
                last_package = directory[index + 1:]
                package_name = base_plugin_package + '.' + directory[:index].replace('/', '.')
            entry_point = self.plugin_util.setup_plugin_configuration(package_name, plugin_directory + '/' + directory)
            if entry_point is not None:
                plugin_name, plugin_ext = os.path.splitext(entry_point)
                # Importing the module will cause IPluginRegistry to invoke it's __init__ fun
                import_target_module = f'.{last_package}.{plugin_name}'
                self._logger.debug(f'import_target_module:{import_target_module}')
                module = import_module(import_target_module, package_name)
                self.__check_loaded_plugin_state(module)
            else:
                self._logger.warning(f'No valid plugin found in {package_name}')

    def discover_plugins(self, plugins: List[str]):
        self.modules.clear()
        IPluginRegistry.plugin_registries.clear()
        self.__search_for_plugins_in(plugins)

    @staticmethod
    def register_plugin(module: type, logger: Logger, meta: Meta) -> PluginCore:
        """
        Create a plugin instance from the given module
        :param module: module to initialize
        :param logger: logger for the module to use
        :param meta: plugin base info
        :return: a high level plugin
        """
        return module(logger, meta)

    @staticmethod
    def hook_plugin(plugin: PluginCore):
        """
        Return a function accepting commands.
        """
        return plugin.invoke
