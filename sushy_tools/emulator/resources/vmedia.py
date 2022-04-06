# Copyright 2019 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import os
import re
import tempfile
from urllib import parse as urlparse

import requests

from sushy_tools.emulator import memoize
from sushy_tools.emulator.resources import base
from sushy_tools import error


DeviceInfo = collections.namedtuple(
    'DeviceInfo',
    ['image_name', 'image_url', 'inserted', 'write_protected',
     'username', 'password', 'verify'])
Certificate = collections.namedtuple(
    'Certificate',
    ['id', 'string', 'type_'])

_CERT_ID = "Default"


class StaticDriver(base.DriverBase):
    """Redfish virtual media simulator."""

    def __init__(self, config, logger):
        super().__init__(config, logger)
        self._devices = memoize.PersistentDict()
        if hasattr(self._devices, 'make_permanent'):
            self._devices.make_permanent(
                self._config.get('SUSHY_EMULATOR_STATE_DIR'), 'vmedia')

        device_types = self._config.get(
            'SUSHY_EMULATOR_VMEDIA_DEVICES')
        if device_types is None:
            device_types = {
                u'Cd': {
                    u'Name': u'Virtual CD',
                    u'MediaTypes': [
                        u'CD',
                        u'DVD'
                    ]
                },
                u'Floppy': {
                    u'Name': u'Virtual Removable Media',
                    u'MediaTypes': [
                        u'Floppy',
                        u'USBStick'
                    ]
                }
            }

        self._device_types = device_types

    def _get_device(self, identity, device):
        try:
            return self._devices[(identity, device)]

        except KeyError:
            self._devices.update(
                {(identity, k): v for k, v in self._device_types.items()})

        try:
            return self._devices[(identity, device)]

        except KeyError:
            raise error.NotFound(
                'No such virtual media device %s owned by resource '
                '%s' % (device, identity))

    @property
    def driver(self):
        """Return human-friendly driver information

        :returns: driver information as `str`
        """
        return '<static-vmedia>'

    @property
    def devices(self):
        """Return available Redfish virtual media devices

        :returns: list of virtual media devices IDs
        """
        return list(self._device_types)

    def get_device_name(self, identity, device):
        """Get virtual media device name

        :param identity: parent resource ID
        :param device: device name
        :returns: virtual media device name
        :raises: `error.FishyError`
        """
        device_info = self._get_device(identity, device)
        return device_info.get('Name', identity)

    def get_device_media_types(self, identity, device):
        """Get supported media types for the device

        :param identity: parent resource ID
        :param device: device name
        :returns: media types supported by this device
        :raises: `error.FishyError`
        """
        device_info = self._get_device(identity, device)
        return device_info.get('MediaTypes', [])

    def get_device_image_info(self, identity, device):
        """Get media state of the virtual media device

        :param identity: parent resource ID
        :param device: device name
        :returns: a `DeviceInfo` with: image name, image path,
            `True` is media is inserted, `True` if media is write-protected,
            user name and password
        :raises: `error.FishyError`
        """
        device_info = self._get_device(identity, device)

        return DeviceInfo(device_info.get('ImageName', ''),
                          device_info.get('Image', ''),
                          device_info.get('Inserted', False),
                          device_info.get('WriteProtected', False),
                          device_info.get('UserName', ''),
                          device_info.get('Password', ''),
                          device_info.get('Verify', False))

    def update_device_info(self, identity, device, verify=False):
        """Update the virtual media device

        :param identity: parent resource ID
        :param device: device name
        :param verify: new value for VerifyCertificate
        :raises: `error.FishyError`
        """
        device_info = self._get_device(identity, device)
        device_info['Verify'] = verify
        self._devices[(identity, device)] = device_info

    def add_certificate(self, identity, device, cert_string, cert_type):
        device_info = self._get_device(identity, device)

        if "Certificate" in device_info:
            raise error.FishyError("Virtual media certificate already exists",
                                   code=409)

        device_info["Certificate"] = {'Type': cert_type, 'String': cert_string}
        self._devices[(identity, device)] = device_info

        return Certificate(_CERT_ID, cert_string, cert_type)

    def replace_certificate(self, identity, device, cert_id,
                            cert_string, cert_type):
        device_info = self._get_device(identity, device)
        if cert_id != _CERT_ID or "Certificate" not in device_info:
            raise error.NotFound(f"Certificate {cert_id} not found")

        device_info["Certificate"] = {'Type': cert_type, 'String': cert_string}
        self._devices[(identity, device)] = device_info

        return Certificate(_CERT_ID, cert_string, cert_type)

    def list_certificates(self, identity, device):
        device_info = self._get_device(identity, device)
        try:
            certificate = device_info["Certificate"]
        except KeyError:
            return []

        return [Certificate(_CERT_ID, certificate['String'],
                            certificate['Type'])]

    def delete_certificate(self, identity, device, cert_id):
        device_info = self._get_device(identity, device)
        if cert_id != _CERT_ID or "Certificate" not in device_info:
            raise error.NotFound(f"Certificate {cert_id} not found")

        del device_info["Certificate"]
        self._devices[(identity, device)] = device_info

    def _write_from_response(self, image_url, rsp, tmp_file):
        with open(tmp_file.name, 'wb') as fl:
            for chunk in rsp.iter_content(chunk_size=8192):
                if chunk:
                    fl.write(chunk)

        local_file = None

        content_dsp = rsp.headers.get('content-disposition')
        if content_dsp:
            local_file = re.findall('filename="(.+)"', content_dsp)

        if local_file:
            local_file = local_file[0]

        if not local_file:
            parsed_url = urlparse.urlparse(image_url)
            local_file = os.path.basename(parsed_url.path)

        if not local_file:
            local_file = 'image.iso'

        return local_file

    def insert_image(self, identity, device, image_url,
                     inserted=True, write_protected=True,
                     username=None, password=None):
        """Upload, remove or insert virtual media

        :param identity: parent resource ID
        :param device: device name
        :param image_url: URL to ISO image to place into `device` or `None`
            to eject currently present media
        :param inserted: treat currently present media as inserted or not
        :param write_protected: prevent write access the inserted media
        :raises: `FishyError` if image can't be manipulated
        """
        device_info = self._get_device(identity, device)
        verify_media_cert = device_info.get(
            'Verify',
            # NOTE(dtantsur): it's de facto standard for Redfish to default
            # to no certificate validation.
            self._config.get('SUSHY_EMULATOR_VMEDIA_VERIFY_SSL', False))
        custom_cert = None
        if verify_media_cert:
            try:
                custom_cert = device_info['Certificate']['String']
            except KeyError:
                self._logger.debug(
                    'TLS verification is enabled but not custom certificate '
                    'is provided, using built-in CA for manager %s, virtual '
                    'media device %s', identity, device)
            else:
                self._logger.debug(
                    'Using a custom TLS certificate for manager %s, virtual '
                    'media device %s', identity, device)

        auth = (username, password) if (username and password) else None

        if custom_cert is not None:
            custom_cert_file = tempfile.NamedTemporaryFile(mode='wt')
            custom_cert_file.write(custom_cert)
            custom_cert_file.flush()
            verify_media_cert = custom_cert_file.name

        try:
            with requests.get(image_url,
                              stream=True,
                              auth=auth,
                              verify=verify_media_cert) as rsp:
                if rsp.status_code >= 400:
                    self._logger.error(
                        'Failed fetching image from URL %s: '
                        'got HTTP error %s:\n%s',
                        image_url, rsp.status_code, rsp.text)
                    target_code = 502 if rsp.status_code >= 500 else 400
                    raise error.FishyError(
                        "Cannot download virtual media: got error %s "
                        "from the server" % rsp.status_code,
                        code=target_code)

                with tempfile.NamedTemporaryFile(
                        mode='w+b', delete=False) as tmp_file:

                    local_file = self._write_from_response(image_url,
                                                           rsp, tmp_file)
                    temp_dir = tempfile.mkdtemp(
                        dir=os.path.dirname(tmp_file.name))
                    local_file_path = os.path.join(temp_dir, local_file)

                os.rename(tmp_file.name, local_file_path)
        except error.FishyError as ex:
            msg = 'Failed fetching image from URL %s: %s' % (image_url, ex)
            self._logger.error(msg)
            raise  # leave the original error intact (code, etc)
        except Exception as ex:
            msg = 'Failed fetching image from URL %s: %s' % (image_url, ex)
            self._logger.exception(msg)
            raise error.FishyError(msg)
        finally:
            if custom_cert is not None:
                custom_cert_file.close()

        self._logger.debug(
            'Fetched image %(url)s for %(identity)s' % {
                'identity': identity, 'url': image_url})

        device_info['Image'] = image_url
        device_info['ImageName'] = local_file
        device_info['Inserted'] = inserted
        device_info['WriteProtected'] = write_protected
        device_info['UserName'] = username or ''
        device_info['Password'] = password or ''
        device_info['_local_file'] = local_file_path

        self._devices.update({(identity, device): device_info})

        return local_file_path

    def eject_image(self, identity, device):
        """Eject virtual media image

        :param identity: parent resource ID
        :param device: device name
        :raises: `FishyError` if image can't be manipulated
        """
        device_info = self._get_device(identity, device)

        device_info['Image'] = ''
        device_info['ImageName'] = ''
        device_info['Inserted'] = False
        device_info['WriteProtected'] = False
        device_info['UserName'] = ''
        device_info['Password'] = ''

        self._devices.update({(identity, device): device_info})

        local_file = device_info.pop('_local_file', None)
        if local_file:
            os.unlink(local_file)

            self._logger.debug(
                'Removed local file %(file)s for %(identity)s' % {
                    'identity': identity, 'file': local_file})
