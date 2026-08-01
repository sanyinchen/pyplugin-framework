import logging
import os
import sys
from logging import Logger, StreamHandler, DEBUG
from typing import Union, Optional
from colorlog import ColoredFormatter
import yaml
import os
import shutil
import urllib.request
import zipfile


class FileSystem:

    @staticmethod
    def __get_base_dir():
        """At most all application packages are just one level deep"""
        current_path = os.path.abspath(os.path.dirname(__file__))
        return os.path.join(current_path, '..')

    @staticmethod
    def get_base_name(file_path):
        return os.path.basename(file_path)

    @staticmethod
    def __get_config_directory() -> str:
        base_dir = FileSystem.__get_base_dir()
        return os.path.join(base_dir, 'settings')

    @staticmethod
    def get_plugins_directory() -> str:
        base_dir = FileSystem.__get_base_dir()
        return os.path.join(base_dir)

    @staticmethod
    def get_plugin_path_by_package_name(package_name) -> str:
        return package_name.replace(".", "/")

    @staticmethod
    def load_configuration(name: str, config_directory: Optional[str] = None) -> dict:
        if config_directory is None:
            config_directory = FileSystem.__get_config_directory()
        with open(os.path.join(config_directory, name)) as file:
            input_data = yaml.safe_load(file)
        return input_data

    @staticmethod
    def clean_folder(directory):
        # 获取指定目录下的所有文件和文件夹
        for filename in os.listdir(directory):
            filepath = os.path.join(directory, filename)
            # 判断是否是文件
            if os.path.isfile(filepath):
                # 删除文件
                os.unlink(filepath)

    @staticmethod
    def create_folder(folder_path: str):
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

    @staticmethod
    def reset_folder(folder_path: str):
        try:
            FileSystem.clean_folder(folder_path)
        except:
            pass
        FileSystem.create_folder(folder_path)

    @staticmethod
    def download_file(url, save_path):
        try:
            urllib.request.urlretrieve(url, save_path)
            return {
                'error_no': 0,
                'error_msg': '',
                'path': save_path
            }
        except Exception as e:
            return {
                'error_no': -1,
                'error_msg': str(e),
                'path': ''
            }

    @staticmethod
    def unzip_file(zip_path, extract_dir):
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

    @staticmethod
    def get_all_files(directory):
        all_files = []
        for root, dirs, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                all_files.append(file_path)
        return all_files

    @staticmethod
    def write_list_to_file(file_path, data_list, chunk_size=1024 * 1024 * 1024):
        with open(file_path, 'w') as file:
            for i in range(0, len(data_list), chunk_size):
                chunk = data_list[i:i + chunk_size]
                file.write('\n'.join(chunk) + '\n')


class LogUtil(Logger):
    def __init__(
            self,
            name: str,
            level: Union[int, str] = DEBUG,
            *args,
            **kwargs
    ) -> None:
        super().__init__(name, level)
        # 创建并配置 ConsoleHandler
        console_handler = logging.StreamHandler()
        # 设置自定义 Formatter
        #  '%(asctime)s【%(levelname)s】%(funcName)s:%(lineno)d—%(message)s',
        formatter = ColoredFormatter(
            "%(asctime)s %(log_color)s%(levelname)s:%(funcName)s:%(lineno)d:%(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            reset=True,
            log_colors={
                'DEBUG': 'white',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'red,bg_yellow',
            }
        )
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG)
        self.addHandler(console_handler)
        # self.addHandler(self.__get_stream_handler())

    def __get_stream_handler(self) -> StreamHandler:
        handler = StreamHandler(sys.stdout)
        handler.setFormatter(self.formatter)
        return handler

    @staticmethod
    def create(log_level: str = 'DEBUG') -> Logger:
        logging.setLoggerClass(LogUtil)
        logger = logging.getLogger('log')
        logger.setLevel(log_level)
        return logger
