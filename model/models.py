from dataclasses import dataclass


@dataclass
class PluginRunTimeOption(object):
    main: str



class PluginContext:
    data = {}

    def set_config(self, common_data):
        self.data['config'] = common_data

    def get_config(self):
        return self.data['config']

    def get_plugin_data(self, plugin_name):
        if not self.data.get(plugin_name):
            return None
        return self.data[plugin_name]

    def set_plugin_data(self, plugin_name, data) -> None:
        self.data[plugin_name] = data


@dataclass
class DependencyModule:
    name: str
    version: str

    def __str__(self) -> str:
        return f'{self.name}=={self.version}'


@dataclass
class PluginConfig:
    runtime: PluginRunTimeOption


@dataclass
class Meta:
    name: str

    def __str__(self) -> str:
        return f'plugin name:{self.name}'


@dataclass
class Pair:
    type: type
    meta: Meta


@dataclass
class PluginInvokeResponse:
    PLUGIN_INVOKE_OK = 0

    data = {}
    errorNo: int
    errorMsg: str = ''

    @staticmethod
    def create_succeed_response(data):
        res = PluginInvokeResponse(PluginInvokeResponse.PLUGIN_INVOKE_OK)
        res.data = data
        return res

    @staticmethod
    def create_failed_response(error_no: int, error_msg: str):
        return PluginInvokeResponse(error_no, error_msg)

    def is_succeed(self) -> bool:
        return self.errorNo == self.PLUGIN_INVOKE_OK
