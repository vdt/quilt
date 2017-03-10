"""
Build: parse and add user-supplied files to store
"""
import os
import re

import requests

try:
    import fastparquet
except ImportError:
    fastparquet = None

try:
    import pyarrow as pa
    from pyarrow import parquet
except ImportError:
    pa = None

try:
    from pyspark.sql import SparkSession
except ImportError:
    SparkSession = None

from .const import PackageFormat

from .package import Package, HDF5Package

# start with alpha (_ may clobber attrs), continue with alphanumeric or _
VALID_NAME_RE = re.compile(r'^[a-zA-Z]\w*$')
CHUNK_SIZE = 4096

class StoreException(Exception):
    """
    Exception class for store I/O
    """
    pass


class PackageStore(object):
    """
    Base class for managing Quilt data package repositories. This
    class and its subclasses abstract file formats, file naming and
    reading and writing to/from data files.
    """
    PACKAGE_DIR_NAME = 'quilt_packages'
    PACKAGE_FILE_EXT = '.json'

    def __init__(self, startpath='.'):
        self._start_dir = startpath

    def find_package_dirs(self):
        """
        Walks up the directory tree and looks for `quilt_packages` directories
        in the ancestors of the starting directory.

        The algorithm is the same as Node's `node_modules` algorithm
        ( https://nodejs.org/docs/v7.4.0/api/modules.html#modules_all_together ),
        except that it doesn't stop at the top-level `quilt_packages` directory.

        Returns a (possibly empty) generator.
        """
        path = os.path.realpath(self._start_dir)
        while True:
            parent_path, name = os.path.split(path)
            if name != self.PACKAGE_DIR_NAME:
                package_dir = os.path.join(path, self.PACKAGE_DIR_NAME)
                if os.path.isdir(package_dir):
                    yield package_dir
            if parent_path == path:  # The only reliable way to detect the root.
                break
            path = parent_path 

    def get_package(self, user, package):
        """
        Finds an existing package in one of the package directories.
        """
        self._path = None
        self._pkg_dir = None
        if not VALID_NAME_RE.match(user):
            raise StoreException("Invalid user name: %r" % user)
        if not VALID_NAME_RE.match(package):
            raise StoreException("Invalid package name: %r" % package)

        pkg_dirs = self.find_package_dirs()
        for package_dir in pkg_dirs:
            path = os.path.join(package_dir, user, package + self.PACKAGE_FILE_EXT)
            if os.path.exists(path):
                return HDF5Package(user=user,
                                   package=package,
                                   mode='r',
                                   path=path,
                                   pkg_dir=package_dir)
        return None

    def create_package(self, user, package, format):
        """
        Creates a new package in the innermost `quilt_packages` directory
        (or in a new `quilt_packages` directory in the current directory)
        and allocates a per-user directory if needed.
        """
        if not VALID_NAME_RE.match(user):
            raise StoreException("Invalid user name: %r" % user)
        if not VALID_NAME_RE.match(package):
            raise StoreException("Invalid package name: %r" % package)

        package_dir = next(self.find_package_dirs(), self.PACKAGE_DIR_NAME)
        user_path = os.path.join(package_dir, user)
        if not os.path.isdir(user_path):
            os.makedirs(user_path)
        obj_path = os.path.join(package_dir, Package.OBJ_DIR)
        if not os.path.isdir(obj_path):
            os.makedirs(obj_path)
        path = os.path.join(user_path, package + self.PACKAGE_FILE_EXT)

        # TODO: Check format and create appropriate Package subclass
        return HDF5Package(user=user,
                           package=package,
                           mode='w',
                           path=path,
                           pkg_dir=package_dir)


def ls_packages(pkg_dir):
    """
    List all packages from all package directories.
    """
    pkg_format = PackageFormat(os.environ.get('QUILT_PACKAGE_FORMAT', PackageFormat.default.value))
    if pkg_format is PackageFormat.HDF5:
        packages = HDF5PackageStore.ls_packages(pkg_dir)
    elif pkg_format is PackageFormat.FASTPARQUET:
        packages = FastParquetPackageStore.ls_packages(pkg_dir)
    else:
        raise StoreException("Unsupported Package Format %s" % pkg_format)
    return packages
