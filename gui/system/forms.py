#+
# Copyright 2010 iXsystems
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
# $FreeBSD$
#####################################################################

from django.utils.translation import ugettext_lazy as _
from django.shortcuts import render_to_response                
from freenasUI.system.models import *                         
from freenasUI.middleware.notifier import notifier
from django.http import HttpResponseRedirect
from django.utils.safestring import mark_safe
from django.utils.encoding import force_unicode 
from freenasUI.common.forms import ModelForm
from freenasUI.common.forms import Form
from dojango.forms import fields, widgets 
from dojango.forms.fields import BooleanField 
from dojango import forms
# TODO: dojango.forms.FileField seems to have some bug that mangles the interface
# so we use django.forms.FileField for this release.
import django.forms

class SettingsForm(ModelForm):
    class Meta:
        model = Settings
    def save(self):
        super(SettingsForm, self).save()
        notifier().reload("timeservices")

class AdvancedForm(ModelForm):
    class Meta:
        model = Advanced

class EmailForm(ModelForm):
    em_pass1 = forms.CharField(label=_("Password"), widget=forms.PasswordInput)
    em_pass2 = forms.CharField(label=_("Password confirmation"), widget=forms.PasswordInput,
        help_text = _("Enter the same password as above, for verification."))
    class Meta:
        model = Email
        exclude = ('em_pass',)
    def __init__(self, *args, **kwargs):
        super(EmailForm, self).__init__( *args, **kwargs)
        try:
            self.fields['em_pass1'].initial = self.instance.em_pass
            self.fields['em_pass2'].initial = self.instance.em_pass
        except:
            pass
    def clean_em_pass2(self):
        pass1 = self.cleaned_data.get("em_pass1", "")
        pass2 = self.cleaned_data.get("em_pass2", None)
        if pass1 != pass2:
            raise forms.ValidationError(_("The two password fields didn't match."))
        return pass2
    def save(self, commit=True):
        email = super(EmailForm, self).save(commit=False)
        if commit:
             email.em_pass = self.cleaned_data['em_pass2']
             email.save()
             notifier().start("ix-msmtp")
        return email

class FirmwareForm(Form):
    mountpoint = forms.ChoiceField(label="Place to temporarily place firmware file", help_text="The system will use this place to temporarily store the firmware file before it's being applied.",choices=(), widget=forms.Select(attrs={ 'class': 'required' }),)
    firmware = django.forms.FileField(label="New image to be installed")
    def __init__(self, *args, **kwargs):
        from freenasUI.storage.models import MountPoint
        super(FirmwareForm, self).__init__(*args, **kwargs)
        self.fields['mountpoint'].choices = [(x.mp_path, x.mp_path) for x in MountPoint.objects.all()]
    def clean(self):
        cleaned_data = self.cleaned_data
        filename = "%s/firmware.xz" % (cleaned_data["mountpoint"])
        fw = open(filename, 'wb+')
        for c in self.files['firmware'].chunks():
            fw.write(c)
        fw.close()
        retval = notifier().validate_xz(filename)
        if retval:
            raise ValueError("Not implemented yet")
        else:
            msg = u"Invalid firmware"
            self._errors["firmware"] = self.error_class([msg])
            del cleaned_data["firmware"]
        return cleaned_data

