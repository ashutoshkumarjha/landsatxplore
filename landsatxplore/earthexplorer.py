"""Handle login and downloading from the EarthExplorer portal."""

import os
import re

import requests
from tqdm import tqdm
import random
import string
import threading
from landsatxplore.api import API
from landsatxplore.errors import EarthExplorerError
from landsatxplore.util import guess_dataset, is_display_id


EE_URL = "https://earthexplorer.usgs.gov/"
EE_LOGIN_URL = "https://ers.cr.usgs.gov/login/"
EE_LOGOUT_URL = "https://earthexplorer.usgs.gov/logout"
EE_DOWNLOAD_URL = (
    "https://earthexplorer.usgs.gov/download/{data_product_id}/{entity_id}/EE/"
)


# IDs of GeoTIFF data product for each dataset
DATA_PRODUCTS = {
    "landsat_tm_c1": "5e83d08fd9932768",
    "landsat_etm_c1": "5e83a507d6aaa3db",
    "landsat_8_c1": "5e83d0b84df8d8c2",
    "landsat_tm_c2_l1": "5e83d0a0f94d7d8d",
    "landsat_etm_c2_l1": "5e83d0d0d2aaa488",
    "landsat_ot_c2_l1": "5e81f14ff4f9941c",
    "landsat_tm_c2_l2": "5e83d11933473426",
    "landsat_etm_c2_l2": "5e83d12aada2e3c5",
    "landsat_ot_c2_l2": "5e83d14f30ea90a9",
    "sentinel_2a": "5e83a42c6eba8084",
}

FILETYPES = {
    'bundle':0,
    'band':1,
    'all':3
}


def _get_tokens(body):
    """Get `csrf_token` and `__ncforminfo`."""
    csrf = re.findall(r'name="csrf" value="(.+?)"', body)[0]
    ncform = re.findall(r'name="__ncforminfo" value="(.+?)"', body)[0]

    if not csrf:
        raise EarthExplorerError("EE: login failed (csrf token not found).")
    if not ncform:
        raise EarthExplorerError("EE: login failed (ncforminfo not found).")

    return csrf, ncform


class EarthExplorer(object):
    """Access Earth Explorer portal."""

    def __init__(self, username, password):
        """Access Earth Explorer portal."""
        self.session = requests.Session()
        self.login(username, password)
        self.api = API(username, password)
        self.maxthreads = 5 # Threads count for downloads
        self.sema = threading.Semaphore(value=self.maxthreads)
        self.threads = []

    def logged_in(self):
        """Check if the log-in has been successfull based on session cookies."""
        eros_sso = self.session.cookies.get("EROS_SSO_production_secure")
        return bool(eros_sso)

    def login(self, username, password):
        """Login to Earth Explorer."""
        rsp = self.session.get(EE_LOGIN_URL)
        csrf, ncform = _get_tokens(rsp.text)
        payload = {
            "username": username,
            "password": password,
            "csrf": csrf,
            "__ncforminfo": ncform,
        }
        rsp = self.session.post(EE_LOGIN_URL, data=payload, allow_redirects=True)

        if not self.logged_in():
            raise EarthExplorerError("EE: login failed.")

    def logout(self):
        """Log out from Earth Explorer."""
        self.session.get(EE_LOGOUT_URL)

    def _download(self, url, output_dir, timeout, chunk_size=1024, skip=False):
        """Download remote file given its URL."""
        # Check availability of the requested product
        # EarthExplorer should respond with JSON
        with self.session.get(
            url, allow_redirects=False, stream=True, timeout=timeout
        ) as r:
            r.raise_for_status()
            error_msg = r.json().get("errorMessage")
            if error_msg:
                raise EarthExplorerError(error_msg)
            download_url = r.json().get("url")

        try:
            with self.session.get(
                download_url, stream=True, allow_redirects=True, timeout=timeout
            ) as r:
                file_size = int(r.headers.get("Content-Length"))
                with tqdm(
                    total=file_size, unit_scale=True, unit="B", unit_divisor=1024
                ) as pbar:
                    local_filename = r.headers["Content-Disposition"].split("=")[-1]
                    local_filename = local_filename.replace('"', "")
                    local_filename = os.path.join(output_dir, local_filename)
                    if skip:
                        return local_filename
                    with open(local_filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                pbar.update(chunk_size)
        except requests.exceptions.Timeout:
            raise EarthExplorerError(
                "Connection timeout after {} seconds.".format(timeout)
            )
        return local_filename

    def download(self, identifier, output_dir, dataset=None, timeout=300, skip=False):
        """Download a Landsat scene.

        Parameters
        ----------
        identifier : str
            Scene Entity ID or Display ID.
        output_dir : str
            Output directory. Automatically created if it does not exist.
        dataset : str, optional
            Dataset name. If not provided, automatically guessed from scene id.
        timeout : int, optional
            Connection timeout in seconds.
        skip : bool, optional
            Skip download, only returns the remote filename.

        Returns
        -------
        filename : str
            Path to downloaded file.
        """
        os.makedirs(output_dir, exist_ok=True)
        if not dataset:
            dataset = guess_dataset(identifier)
        if is_display_id(identifier):
            entity_id = self.api.get_entity_id(identifier, dataset)
        else:
            entity_id = identifier
        url = EE_DOWNLOAD_URL.format(
            data_product_id=DATA_PRODUCTS[dataset], entity_id=entity_id
        )
        filename = self._download(url, output_dir, timeout=timeout, skip=skip)
        return filename

    def resume_download(self,output_dir,timeout,chunk_size,skip,download_url):
        self.sema.acquire() 
        try:
            with self.session.get(
                download_url, stream=True, allow_redirects=True, timeout=timeout
            ) as rh:
                file_size = int(rh.headers.get("Content-Length",0))
                local_filename = rh.headers["Content-Disposition"].split("=")[-1]
                local_filename = local_filename.replace('"', "")
                if local_filename.endswith('tar'):
                    local_filename=local_filename+".gz"
                local_filename = os.path.join(output_dir, local_filename)
                resume_byte_pos=self.checkFileSize(local_filename)
                initial_pos = resume_byte_pos if resume_byte_pos else 0
                mode = 'ab' if resume_byte_pos else 'wb' 
                resume_header = ({'Range': f'bytes={resume_byte_pos}-'} if resume_byte_pos else None)
                with self.session.get(
                    download_url, stream=True, allow_redirects=True, timeout=timeout, headers=resume_header
                ) as r:
                    with open(local_filename, mode) as f:
                        with tqdm(total=file_size, unit='B',unit_scale=True, unit_divisor=1024,desc=local_filename, 
                                    initial=initial_pos, miniters=1) as pbar:
                            for chunk in r.iter_content(chunk_size=chunk_size):
                                if chunk:
                                    f.write(chunk)
                                    pbar.update(chunk_size)
            self.sema.release()
        except requests.exceptions.Timeout:
            raise EarthExplorerError(
                "Connection timeout after {} seconds.".format(timeout)
            )
            self.sema.release()
            self.runDownloadMultiThread(threads, output_dir, timeout, chunk_size, skip, download_url)
        return local_filename

    def checkFileSize(self,local_filename):
        if os.path.isfile(local_filename):
            return os.stat(local_filename).st_size
        return None

    def _downloadFileMultiThread(self,output_dir,timeout,chunk_size,skip,download_url):
        self.sema.acquire()
        local_filename=None
        try:
            with self.session.get(
                download_url, stream=True, allow_redirects=True, timeout=timeout
            ) as r:
                file_size = int(r.headers.get("Content-Length"))
                with tqdm(
                    total=file_size, unit_scale=True, unit="B", unit_divisor=1024
                ) as pbar:
                    local_filename = r.headers["Content-Disposition"].split("=")[-1]
                    local_filename = local_filename.replace('"', "")+".gz"
                    local_filename = os.path.join(output_dir, local_filename)
                    if skip:
                        return local_filename
                    with open(local_filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                pbar.update(chunk_size)
            self.sema.release()
        except requests.exceptions.Timeout:
            raise EarthExplorerError(
                "Connection timeout after {} seconds.".format(timeout)
            )
            self.sema.release()
            self.runDownloadMultiThread(threads, output_dir, timeout, chunk_size, skip, download_url)
        return local_filename
  
    def runDownloadMultiThread(self,threads, output_dir,timeout,chunk_size,skip,url):
        #thread = threading.Thread(target=self._downloadFileMultiThread, args=(output_dir,timeout,chunk_size,skip,url,))
        thread = threading.Thread(target=self.resume_download, args=(output_dir,timeout,chunk_size,skip,url,))
        self.threads.append(thread)
        thread.start()

    def downloadbulk(self,entityfile, output_dir,dataset=None, filetype="bundle",idField="displayId",timeout=300, chunk_size=1024,skip=False):
        """Download a Landsat scene.

        Parameters
        ----------
        entityfile : str
            Scene Entity ID or Display ID File Containing their entries.
        dataset : str, optional
                    Dataset name. If not provided, automatically guessed from scene id.
        output_dir : str
            Output directory. Automatically created if it does not exist.
        filetype : str 
            File type as 'bundle' or 'bandd' for individual files 
        idFiled : str
            Files contains the 'entityId' or 'displayId'
        timeout : int, optional
            Connection timeout in seconds.
        skip : bool, optional
            Skip download, only returns the remote filename list.

        Returns
        -------
        filename : str
            Path to downloaded file.
        """

        try:
            with open(entityfile, 'r') as f:
                entityList = f.readlines()   
            if(len(entityList)<1):
                raise EarthExplorerError(
                    "Entity File:{entityfile} is Empty.".format(entityfile)
                )
        except FileNotFoundError as e:
            raise EarthExplorerError(
                "Entity File:{entityfile} not found.".format(entityfile)
            )
        productsdownloads=self.api.get_products_download_options(entityList=entityList,filetype=filetype,datasetName=dataset,idField=idField)
        #print(productsdownloads)
        productsUrls=self.api.get_download_urls(productsdownloads)
        if skip :
            return productsUrls
        #print(productsUrls)
        for productsUrl in productsUrls:
            self.runDownloadMultiThread(self.threads, output_dir,timeout,chunk_size,skip,productsUrl) 

        print("Download Started\n")
        for thread in self.threads:
            thread.join()


