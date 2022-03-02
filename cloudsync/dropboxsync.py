#!/usr/bin/env python

import logging
import contextlib

import datetime
import dropbox
from dropbox.files import FileMetadata
from dropbox.exceptions import ApiError
import os
from pathlib import Path
import time
from typing import List, Optional
import unicodedata

from sync_file import SyncFile
from sync_file.file_handler import DropboxFileHandler, FileType

class DropboxSync(object):
    """
    Class to help synchronize files to/from dropbox
    use Dropbox API v2 (https://github.com/dropbox/dropbox-sdk-python)
    """

    locList: List[SyncFile] = []
    dbList: List[SyncFile] = []
    filterItems: List[SyncFile] = []
    sourceFilesMatched: List[SyncFile] = []
    dbx: Optional[dropbox.Dropbox] = None

    def __init__(self, **kwargs):
        self.args = kwargs
        self.localDir = Path(kwargs['localdir'])
        self.dropboxDir = Path(kwargs['dropboxdir'])
        self.directionToDb = kwargs['direction'] == 'todropbox'

        self.timeoutSec = 2 * 60
        self.logger = logging.getLogger(__name__)
        self.logger.addHandler(logging.NullHandler())

    def setLogger(self, logger):
        self.logger = logger

    # prepare
    def prepare(self):
        self.logger.info('--- Mode: %s' % self.args['direction'])
        self.prepareDropboxAuth()
        self.checkDropboxAuth()
        self.checkDropboxDir()
        self.checkLocalDir()

        self.listDropboxFiles()
        self.listLocalFiles()
        self.listFilterItems()

    def prepareDropboxAuth(self):
        self.logger.debug('Connecting to dropbox using token...')
        self.dbx = dropbox.Dropbox(self.args['token'])
        self.logger.debug('Dropbox connected')

    def checkLocalDir(self):
        if not os.path.exists(self.localDir):
            raise Exception('Local path is not exists:%s' % self.localDir)
        if not os.path.isdir(self.localDir):
            raise Exception('Local path is not directory:%s' % self.localDir)

    def checkDropboxAuth(self):
        """
        Checks Dropbox uploader is initialized to Dropbox account
        """
        self.logger.debug('Getting info about dropbox account...')
        acc = self.dbx.users_get_current_account()
        self.logger.debug('Dropbox account: [%s_%s] mail:%s' % (acc.country, acc.locale, acc.email))

    def checkDropboxDir(self):
        """
        Checks that Dropbox folder exists.
        """
        self.logger.debug('Checking if Dropbox folder exists...')
        try:
            self.dbx.files_list_folder(str(self.dropboxDir))
            self.logger.debug('Dropbox folder exists')
        except:
            self.logger.debug(f"Folder {str(self.dropboxDir)} does not exist on Dropbox, creating...")
            self.dbx.files_create_folder_v2(str(self.dropboxDir))

    def listLocalFiles(self):
        self.logger.debug('Getting list of local files...')
        self.locList = [
            SyncFile(self.localDir / Path(unicodedata.normalize('NFC', f)))
            for f in os.listdir(self.localDir)
            if os.path.isfile(self.localDir / f)
        ]
        self.logger.debug(f'Local files: {len(self.locList)}')
        return True

    # filtration
    def listFilterItems(self):
        if self.directionToDb:
            self.filterItems = self.locList
        else:
            self.filterItems = self.dbList

    def filterSourceFiles(self, filters):
        source_count = len(self.filterItems)
        self.logger.debug(f'Source files: {source_count}')
        self.sourceFilesMatched = [f for f in self.filterItems if not f.filter(filters)]
        self.logger.info(f'--- Filter source files: {source_count} -> {len(self.sourceFilesMatched)}')

    # synchronize
    def synchronize(self):
        # for debug
        #self.fixLocalTimestamps()

        if self.directionToDb:
            self.deleteDropboxFiles()
            self.syncToDropbox()
        else:
            self.deleteLocalFiles()
            self.syncToLocal()

        return True

    def deleteLocalFiles(self):
        # remove local
        sourceNames = [fileItem.name for fileItem in self.sourceFilesMatched]
        delList = [fileItem for fileItem in self.locList
                   if fileItem.name not in sourceNames
                   and fileItem.type == FileType.FILE]
        if not delList:
            return
        self.logger.debug(f'Local files to delete: {len(delList)}')
        for fileItem in delList:
            fileItem.delete()
        self.logger.info(f'--- Deleted {len(delList)}/{len(self.locList)} local files')

    def syncToLocal(self):
        countSuccess = 0
        countSkip = 0
        countFails = 0
        for fileItem in self.sourceFilesMatched:

            # self.db_handler.file = self.dropboxDir / fileItem.name

            if fileItem in self.locList:
                self.logger.debug(f'Skip existed: {fileItem.name}')
                countSkip += 1
                continue
            if self.downloadFile(fileItem):
                countSuccess += 1
            else:
                countFails += 1
        # print stat
        strSkip = ' Skip:%d' % countSkip if countSkip else ''
        strFails = ' Fails:%d' % countFails if countFails else ''
        self.logger.info('--- Download %d/%d%s%s' % (countSuccess, len(self.sourceFilesMatched), strSkip, strFails))

    def deleteDropboxFiles(self):
        """ Delete not matched files from Dropbox directory """
        sourceNames = [fileItem.name for fileItem in self.sourceFilesMatched]
        delList = [fileItem for fileItem in self.dbList
                   if fileItem.name not in sourceNames
                   and fileItem.type == FileType.FILE
        ]
        if not delList:
            return
        self.logger.debug('Dropbox files to delete:%s' % len(delList))
        for fileItem in delList:
            self.deleteFile(fileItem)
        self.logger.info('--- Success delete %d/%d dropbox files' % (len(delList), len(self.dbList)))

    def syncToDropbox(self):
        countSuccess = 0
        countSkip = 0
        countFails = 0
        for fileItem in self.sourceFilesMatched:
            if fileItem in self.dbList:
                self.logger.debug('Skip existed:%s' % fileItem)
                countSkip += 1
                continue
            if self.uploadFile(fileItem):
                countSuccess += 1
            else:
                countFails += 1
        # print stat
        strSkip = ' Skip:%d' % countSkip if countSkip else ''
        strFails = ' Fails:%d' % countFails if countFails else ''
        self.logger.info('--- Success upload %d/%d%s%s' % (countSuccess, len(self.sourceFilesMatched), strSkip, strFails))

    # dropbox helpers
    def listDropboxFiles(self):
        """List a folder.
        Return an array of filter items
        """
        self.logger.debug('Downloading dropbox list files...')

        try:
            with self.stopwatch(__name__):
                res = self.dbx.files_list_folder(str(self.dropboxDir))
        except ApiError as err:
            self.dbList = []
            raise Exception('Folder listing failed for %s -- assumed empty:%s' % (str(self.dropboxDir), err))
        else:
            self.logger.debug('Dropbox files:%s' % len(res.entries))
            self.dbList = [SyncFile(self.dropboxDir / dbfile.name, file_handler=DropboxFileHandler(self.dbx)) for dbfile in res.entries]
            print(self.dbList)

    def downloadFile(self, file_item: SyncFile):
        """Download a file.
        Return True when success, or False if error occurs.
        """

        db_path = self.dropboxDir / file_item.name
        local_path = self.localDir / file_item.name
        file_size = SyncFile(db_path, file_handler=self.db_handler).size

        self.logger.debug(f'Downloading {file_item.name} ({file_size} bytes) ...')
        with self.stopwatch('downloading'):
            try:
                self.dbx.files_download_to_file(str(local_path), str(db_path))
            except ApiError as err:
                raise Exception(f'{file_item.name} - API error: {err}')
        self.logger.debug(f'Success download - {file_item.name} ({file_size} bytes)')
        return True

    def uploadFile(self, file_item: SyncFile):
        """Upload a file.
        Return the request response, or None in case of error.
        """
        db_path = self.dropboxDir / file_item.name
        local_path = self.localDir / file_item.name
        file_size = file_item.size
        mode = dropbox.files.WriteMode.overwrite

        with open(local_path, 'rb') as f:
            data = f.read()
        self.logger.debug(f'Uploading {file_item.name} ({file_size} bytes) ...')
        with self.stopwatch('uploading'):
            try:
                self.dbx.files_upload(
                    data, str(db_path), mode,
                    client_modified=datetime.datetime.utcfromtimestamp(file_item.mod_time),
                    autorename=False,
                    mute=True)
            except dropbox.exceptions.ApiError as err:
                raise Exception(f'{file_item.name}- API error: {err}')
        self.logger.debug(f'Success upload - {file_item.name} ({file_size} bytes)')
        return True

    def deleteFile(self, file_item: SyncFile):
        self.logger.debug(f'Deleting - \'{file_item.name}\'')
        with self.stopwatch('deleting'):
            try:
                file_item.delete()
            except ApiError as err:
                raise Exception(f'{file_item.name} - API error: {err}')
        self.logger.debug(f'Success delete - {file_item.name}')

    @contextlib.contextmanager
    def stopwatch(self, message):
        """Context manager to print how long a block of code took."""
        t0 = time.time()
        try:
            yield
        finally:
            t1 = time.time()
            self.logger.debug('Total elapsed time for %s: %.3f' % (message, t1 - t0))


