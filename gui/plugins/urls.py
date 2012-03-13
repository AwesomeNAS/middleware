#+
# Copyright 2011 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

from django.conf.urls.defaults import patterns, url
from django.contrib.formtools.wizard import FormWizard

from freenasUI.plugins.forms import (PBIFileWizard, PBITemporaryLocationForm,
    PBIUploadForm, JailInfoForm, JailPBIUploadForm)
from freenasUI.system.forms import FileWizard
from jsonrpc import jsonrpc_site
import freenasUI.plugins.views

urlpatterns = patterns('plugins.views',
    url(r'^home/$', 'plugins_home', name='plugins_home'),
    url(r'^pbiwizard/$', PBIFileWizard(
            [PBITemporaryLocationForm, PBIUploadForm],
            prefix="pbi",
            templates=["plugins/pbiwizard.html"]
        ), name='plugins_pbiwizard'),
    url(r'^plugin/edit/(?P<plugin_id>\d+)/$', 'plugin_edit', name="plugin_edit"),
    url(r'^plugin/info/(?P<plugin_id>\d+)/$', 'plugin_info', name="plugin_info"),
    url(r'^plugin/delete/(?P<plugin_id>\d+)/$', 'plugin_delete', name="plugin_delete"),
    url(r'^jailpbi/$', PBIFileWizard(
            [PBITemporaryLocationForm, JailInfoForm, JailPBIUploadForm],
            prefix="jailpbi",
            templates=["plugins/jailpbi.html"]
        ), name='plugins_jailpbi'),
    url(r'^json/', jsonrpc_site.dispatch, name="jsonrpc_mountpoint"),
    url(r'^(?P<name>[^/]+)/(?P<version>[^/]+)/(?P<path>.+)$', 'plugin_fcgi_client', name="plugin_fcgi_client"),
    )
