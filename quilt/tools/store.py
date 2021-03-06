"""
Build: parse and add user-supplied files to store
"""
import json
import os
import re
from shutil import copyfile
import tempfile
import time
import zlib

import pandas as pd
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

from .const import TargetType, PackageFormat, PACKAGE_DIR_NAME
from .core import decode_node, encode_node, hash_contents, FileNode, GroupNode, TableNode
from .hashing import digest_file

# start with alpha (_ may clobber attrs), continue with alphanumeric or _
VALID_NAME_RE = re.compile(r'^[a-zA-Z]\w*$')
CHUNK_SIZE = 4096
ZLIB_LEVEL = 2  # Maximum level.
ZLIB_METHOD = zlib.DEFLATED  # The only supported one.
ZLIB_WBITS = zlib.MAX_WBITS | 16  # Add a gzip header and checksum.
CONTENTS_FILE = 'contents.json'

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
    PACKAGE_FILE_EXT = '.json'
    BUILD_DIR = 'build'
    OBJ_DIR = 'objs'
    TMP_OBJ_DIR = 'objs/tmp'

    @classmethod
    def find_package_dirs(cls, start='.'):
        """
        Walks up the directory tree and looks for `quilt_packages` directories
        in the ancestors of the starting directory.

        The algorithm is the same as Node's `node_modules` algorithm
        ( https://nodejs.org/docs/v7.4.0/api/modules.html#modules_all_together ),
        except that it doesn't stop at the top-level `quilt_packages` directory.

        Returns a (possibly empty) generator.
        """
        path = os.path.realpath(start)
        while True:
            parent_path, name = os.path.split(path)
            if name != PACKAGE_DIR_NAME:
                package_dir = os.path.join(path, PACKAGE_DIR_NAME)
                if os.path.isdir(package_dir):
                    yield package_dir
            if parent_path == path:  # The only reliable way to detect the root.
                break
            path = parent_path

    def __init__(self, user, package, mode):
        self._user = user
        self._package = package
        self._mode = mode
        self._pkg_dir = None
        self._path = None
        self._find_path_read()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

    def file(self, hash_list):
        """
        Returns the path to an object file that matches the given hash.
        """
        assert isinstance(hash_list, list)
        assert len(hash_list) == 1, "File objects must be contained in one file."
        filehash = hash_list[0]
        objpath = os.path.join(self._pkg_dir, self.OBJ_DIR, filehash)
        return objpath

    def dataframe(self, hash_list):
        """
        Creates a DataFrame from a set of objects (identified by hashes).
        """
        raise NotImplementedError()

    def save_df(self, df, name, path, ext, target):
        """
        Save a DataFrame to the store.
        """
        raise NotImplementedError()

    def save_file(self, srcfile, name, path, target):
        """
        Save a (raw) file to the store.
        """
        self._find_path_write()
        filehash = digest_file(srcfile)
        fullname = name.lstrip('/').replace('/', '.')
        self._add_to_contents(fullname, filehash, '', path, target)
        objpath = os.path.join(self._pkg_dir, self.OBJ_DIR, filehash)
        if not os.path.exists(objpath):
            copyfile(srcfile, objpath)

    def get_contents(self):
        """
        Returns a dictionary with the contents of the package.
        """
        try:
            with open(self._path, 'r') as contents_file:
                contents = json.load(contents_file, object_hook=decode_node)
        except IOError:
            contents = GroupNode(dict())

        return contents

    def clear_contents(self):
        """
        Removes the package's contents file.
        """
        if self._path:
            os.remove(self._path)
        self._path = None

    def save_contents(self, contents):
        """
        Saves an updated version of the package's contents.
        """
        with open(self._path, 'w') as contents_file:
            json.dump(contents, contents_file, default=encode_node, indent=2, sort_keys=True)

    def get(self, path):
        """
        Read a group or object from the store.
        """
        if not self.exists():
            raise StoreException("Package not found")

        key = path.lstrip('/')
        ipath = key.split('/') if key else []
        ptr = self.get_contents()
        path_so_far = []
        for node_name in ipath:
            path_so_far += [node_name]
            ptr = ptr.children.get(node_name)
            if ptr is None:
                raise StoreException("Key {path} Not Found in Package {owner}/{pkg}".format(
                    path="/".join(path_so_far),
                    owner=self._user,
                    pkg=self._package))
        node = ptr

        if isinstance(node, GroupNode):
            return node
        elif isinstance(node, TableNode):
            return self.dataframe(node.hashes)
        elif isinstance(node, FileNode):
            return self.file(node.hashes)
        else:
            assert False, "Unhandled Node {node}".format(node=node)

    def get_hash(self):
        """
        Returns the hash digest of the package data.
        """
        return hash_contents(self.get_contents())

    def get_path(self):
        """
        Returns the path to the package's contents file.
        """
        return self._path

    def exists(self):
        """
        Returns True if the package is already installed.
        """
        return not self._path is None

    def install(self, contents, urls):
        """
        Download and install a package locally.
        """
        self._find_path_write()
        local_filename = self.get_path()
        with open(local_filename, 'w') as contents_file:
            json.dump(contents, contents_file, default=encode_node)

        # Download individual object files and store
        # in object dir. Verify individual file hashes.
        # Verify global hash?

        def install_table(node, urls):
            """
            Downloads and installs the set of objects for one table.
            """
            for download_hash in node.hashes:
                url = urls[download_hash]

                # download and install
                response = requests.get(url, stream=True)
                if not response.ok:
                    msg = "Download {hash} failed: error {code}"
                    raise StoreException(msg.format(hash=download_hash, code=response.status_code))

                local_filename = os.path.join(self._pkg_dir,
                                              self.OBJ_DIR,
                                              download_hash)

                with open(local_filename, 'wb') as output_file:
                    # `requests` will automatically un-gzip the content, as long as
                    # the 'Content-Encoding: gzip' header is set.
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk: # filter out keep-alive new chunks
                            output_file.write(chunk)

                file_hash = digest_file(local_filename)
                if file_hash != download_hash:
                    os.remove(local_filename)
                    raise StoreException("Mismatched hash! Expected %s, got %s." %
                                         (download_hash, file_hash))

        def install_tables(contents, urls):
            """
            Parses package contents and calls install_table for each table.
            """
            for node in contents.children.values():
                if isinstance(node, GroupNode):
                    install_tables(node, urls)
                else:
                    install_table(node, urls)

        return install_tables(contents, urls)

    def _object_path(self, objhash):
        """
        Returns the path to an object file based on its hash.
        """
        return os.path.join(self._pkg_dir, self.OBJ_DIR, objhash)

    def _temporary_object_path(self, name):
        """
        Returns the path to a temporary object, before we know its hash.
        """
        return os.path.join(self._pkg_dir, self.TMP_OBJ_DIR, name)

    class UploadFile(object):
        """
        Helper class to manage temporary package files uploaded by push.
        """
        def __init__(self, store, objhash):
            self._store = store
            self._hash = objhash

        def __enter__(self):
            self._temp_file = tempfile.TemporaryFile()
            with open(self._store._object_path(self._hash), 'rb') as input_file:
                zlib_obj = zlib.compressobj(ZLIB_LEVEL, ZLIB_METHOD, ZLIB_WBITS)
                for chunk in iter(lambda: input_file.read(CHUNK_SIZE), b''):
                    self._temp_file.write(zlib_obj.compress(chunk))
                self._temp_file.write(zlib_obj.flush())
            self._temp_file.seek(0)
            return self._temp_file

        def __exit__(self, type, value, traceback):
            self._temp_file.close()

    def tempfile(self, objhash):
        """
        Create and return a temporary file for uploading to a registry.
        """
        return self.UploadFile(self, objhash)


    def _find_path_read(self):
        """
        Finds an existing package in one of the package directories.
        """
        self._path = None
        self._pkg_dir = None
        if not VALID_NAME_RE.match(self._user):
            raise StoreException("Invalid user name: %r" % self._user)
        if not VALID_NAME_RE.match(self._package):
            raise StoreException("Invalid package name: %r" % self._package)

        pkg_dirs = PackageStore.find_package_dirs()
        for package_dir in pkg_dirs:
            path = os.path.join(package_dir, self._user, self._package + self.PACKAGE_FILE_EXT)
            if os.path.exists(path):
                self._path = path
                self._pkg_dir = package_dir
                return
        return

    def _find_path_write(self):
        """
        Creates a path to store a data package in the innermost `quilt_packages`
        directory (or in a new `quilt_packages` directory in the current directory)
        and allocates a per-user directory if needed.
        """
        if not VALID_NAME_RE.match(self._user):
            raise StoreException("Invalid user name: %r" % self._user)
        if not VALID_NAME_RE.match(self._package):
            raise StoreException("Invalid package name: %r" % self._package)

        package_dir = next(PackageStore.find_package_dirs(), PACKAGE_DIR_NAME)
        for name in [self._user, self.OBJ_DIR, self.TMP_OBJ_DIR]:
            path = os.path.join(package_dir, name)
            if not os.path.isdir(path):
                os.makedirs(path)

        self._path = os.path.join(package_dir, self._user, self._package + self.PACKAGE_FILE_EXT)
        self._pkg_dir = package_dir
        return

    def _add_to_contents(self, fullname, objhash, ext, path, target):
        """
        Adds an object (name-hash mapping) to the package's contents.
        """
        contents = self.get_contents()
        ipath = fullname.split('.')
        leaf = ipath.pop()

        ptr = contents
        for node in ipath:
            ptr = ptr.children.setdefault(node, GroupNode(dict()))

        try:
            target_type = TargetType(target)
            if target_type is TargetType.PANDAS:
                node_cls = TableNode
            elif target_type is TargetType.FILE:
                node_cls = FileNode
            else:
                assert False, "Unhandled TargetType {tt}".format(tt=target_type)
        except ValueError:
            raise StoreException("Unrecognized target {tgt}".format(tgt=target))

        ptr.children[leaf] = node_cls(
            hashes=[objhash],
            metadata=dict(
                q_ext=ext,
                q_path=path,
                q_target=target
            )
        )

        self.save_contents(contents)

    @classmethod
    def ls_packages(cls, pkg_dir):
        """
        List installed packages.
        """
        packages = [
            (user, pkg[:-len(PackageStore.PACKAGE_FILE_EXT)])
            for user in os.listdir(pkg_dir)
            for pkg in os.listdir(os.path.join(pkg_dir, user))
            if pkg.endswith(PackageStore.PACKAGE_FILE_EXT)]
        return packages


class HDF5PackageStore(PackageStore):
    """
    HDF5 Implementation of PackageStore.
    """
    DF_NAME = 'df'

    def __init__(self, user, package, mode):
        super(HDF5PackageStore, self).__init__(user, package, mode)
        self.__store = None

    def dataframe(self, hash_list):
        """
        Creates a DataFrame from a set of objects (identified by hashes).
        """
        assert len(hash_list) == 1, "Multi-file DFs not supported in HDF5."
        filehash = hash_list[0]
        with pd.HDFStore(self._object_path(filehash), 'r') as store:
            return store.get(self.DF_NAME)

    def save_df(self, df, name, path, ext, target):
        """
        Save a DataFrame to the store.
        """
        self._find_path_write()
        buildfile = name.lstrip('/').replace('/', '.')
        storepath = self._temporary_object_path(buildfile)
        with pd.HDFStore(storepath, mode=self._mode) as store:
            store[self.DF_NAME] = df
        filehash = digest_file(storepath)
        self._add_to_contents(buildfile, filehash, ext, path, target)
        os.rename(storepath, self._object_path(filehash))


class FastParquetPackageStore(PackageStore):
    """
    Parquet Implementation of PackageStore.
    """
    def __init__(self, user, package, mode):
        if fastparquet is None:
            raise StoreException("Module fastparquet is required for FastParquetPackageStore.")
        super(FastParquetPackageStore, self).__init__(user, package, mode)

    def save_df(self, df, name, path, ext, target):
        """
        Save a DataFrame to the store.
        """
        self._find_path_write()
        buildfile = name.lstrip('/').replace('/', '.')
        storepath = self._temporary_object_path(buildfile)
        fastparquet.write(storepath, df)

        filehash = digest_file(storepath)
        self._add_to_contents(buildfile, filehash, ext, path, target)
        os.rename(storepath, self._object_path(filehash))

    def dataframe(self, hash_list):
        """
        Creates a DataFrame from a set of objects (identified by hashes).
        """
        assert len(hash_list) == 1, "Multi-file DFs not supported yet."
        filehash = hash_list[0]
        pfile = fastparquet.ParquetFile(self._object_path(filehash))
        return pfile.to_pandas()


class SparkPackageStore(FastParquetPackageStore):
    """
    Spark Implementation of PackageStore.
    """
    def __init__(self, user, package, mode):
        super(SparkPackageStore, self).__init__(user, package, mode)

        if SparkSession is None:
            raise StoreException("Module SparkSession from pyspark.sql is required for " +
                                 "SparkPackageStore.")

    def dataframe(self, hash_list):
        """
        Creates a DataFrame from a set of objects (identified by hashes).
        """
        spark = SparkSession.builder.getOrCreate()
        assert len(hash_list) == 1, "Multi-file DFs not supported yet."
        filehash = hash_list[0]
        df = spark.read.parquet(self._object_path(filehash))
        return df

class ArrowPackageStore(PackageStore):
    """
    Parquet Implementation of PackageStore.
    """

    PACKAGE_FILE_EXT = '.parq'

    def __init__(self, user, package, mode):
        if pa is None:
            raise StoreException("Module pyarrow is required for ArrowPackageStore.")
        super(ArrowPackageStore, self).__init__(user, package, mode)

    def save_df(self, df, name, path, ext, target):
        """
        Save a DataFrame to the store.
        """
        self._find_path_write()

        # Save the dataframe to a local build file
        buildfile = name.lstrip('/').replace('/', '.')
        storepath = self._temporary_object_path(buildfile)
        table = pa.Table.from_pandas(df)
        parquet.write_table(table, storepath)

        # Calculate the file hash and add it to the package contents
        filehash = digest_file(storepath)
        self._add_to_contents(buildfile, filehash, ext, path, target)

        # Move the build file to the object store and rename it to
        # its hash
        objpath = self._object_path(filehash)
        os.rename(storepath, objpath)

    def dataframe(self, hash_list):
        """
        Creates a DataFrame from a set of objects (identified by hashes).
        """
        assert len(hash_list) == 1, "Multi-file DFs not supported for Arrow packages."
        filehash = hash_list[0]

        nt = 8
        fpath = self._object_path(filehash)
        starttime = time.time()
        table = parquet.read_table(fpath, nthreads=nt)
        finishtime = time.time()
        elapsed = finishtime - starttime
        print("Read {path} in {time}s with {nt} threads".format(path=fpath, time=elapsed, nt=nt))

        starttime = time.time()
        df = table.to_pandas()
        finishtime = time.time()
        elapsed = finishtime - starttime
        print("Converted to pandas in {time}s".format(time=elapsed))
        return df


# Helper functions
def get_store(user, package, pkgformat=None, mode='r'):
    """
    Return a PackageStore object of the appropriate type for a
    given data package.
    """
    if not pkgformat:
        pkg_format = PackageFormat(os.environ.get('QUILT_PACKAGE_FORMAT',
                                                  PackageFormat.default.value))

    if pkg_format is PackageFormat.HDF5:
        return HDF5PackageStore(user, package, mode)
    elif pkg_format is PackageFormat.FASTPARQUET:
        return FastParquetPackageStore(user, package, mode)
    elif pkg_format is PackageFormat.SPARK:
        return SparkPackageStore(user, package, mode)
    elif pkg_format is PackageFormat.ARROW:
        return ArrowPackageStore(user, package, mode)
    else:
        raise StoreException("Not Implemented")

def ls_packages(pkg_dir):
    """
    List all packages from all package directories.
    """
    packages = PackageStore.ls_packages(pkg_dir)
    return packages
