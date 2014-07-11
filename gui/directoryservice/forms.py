#+
# Copyright 2014 iXsystems, Inc.
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
import logging
import os
import shutil

from django.forms import FileField
from django.utils.translation import ugettext_lazy as _

from dojango import forms
from freenasUI.common.forms import ModelForm
from freenasUI.common.freenasldap import (
    FreeNAS_ActiveDirectory,
    FreeNAS_LDAP
)
from freenasUI.directoryservice import models
from freenasUI.middleware.notifier import notifier
from freenasUI.services.exceptions import ServiceFailed

log = logging.getLogger('directoryservice.form')


class NT4Form(ModelForm):
    nt4_adminpw2 = forms.CharField(
        max_length=50,
        label=_("Confirm Administrator Password"),
        widget=forms.widgets.PasswordInput(),
        required=False,
    )

    class Meta:
        model = models.NT4
        widgets = {
            'nt4_adminpw': forms.widgets.PasswordInput(render_value=False),
        }
        fields = [
            'nt4_dcname',
            'nt4_netbiosname',
            'nt4_workgroup',
            'nt4_adminname',
            'nt4_adminpw',
            'nt4_adminpw2',
            'nt4_use_default_domain',
            'nt4_enable',
        ]

    def __init__(self, *args, **kwargs):
        super(NT4Form, self).__init__(*args, **kwargs)
        if self.instance.nt4_adminpw:
            self.fields['nt4_adminpw'].required = False
        if self._api is True:
            del self.fields['nt4_adminpw2']

        self.fields["nt4_enable"].widget.attrs["onChange"] = (
            "nt4_mutex_toggle();"
        )

    def clean_nt4_adminpw2(self):
        password1 = self.cleaned_data.get("nt4_adminpw")
        password2 = self.cleaned_data.get("nt4_adminpw2")
        if password1 != password2:
            raise forms.ValidationError(
                _("The two password fields didn't match.")
            )
        return password2

    def clean(self):
        cdata = self.cleaned_data
        if not cdata.get("nt4_adminpw"):
            cdata['nt4_adminpw'] = self.instance.nt4_adminpw
        return cdata

    def save(self):
        enable = self.cleaned_data.get("nt4_enable")
        started = notifier().started("nt4")
        if enable:
            if started == True:
                started = notifier().restart("nt4")
            if started == False:
                started = notifier().start("nt4")
            if started == False:
                self.instance.ad_enable = False
                super(NT4Form, self).save()
                raise ServiceFailed("nt4",
                    _("NT4 failed to reload."))
        else:
            if started == True:
                started = notifier().stop("nt4")

        super(NT4Form, self).save()


class ActiveDirectoryForm(ModelForm):
    ad_certfile = FileField(label=_("Certificate"), required=False)
    ad_keytab = FileField(label=_("Kerberos keytab"), required=False)

    class Meta:
        fields = '__all__'
        model = models.ActiveDirectory
        widgets = {
            'ad_bindpw': forms.widgets.PasswordInput(render_value=False),
        }

    def __original_save(self):
        for name in (
            'ad_domainname',
            'ad_netbiosname',
            'ad_allow_trusted_doms',
            'ad_use_default_domain',
            'ad_use_keytab',
            'ad_keytab',
            'ad_unix_extensions',
            'ad_verbose_logging',
            'ad_bindname',
            'ad_bindpw'
        ):
            setattr(
                self.instance,
                "_original_%s" % name,
                getattr(self.instance, name)
            )

    def __original_changed(self):
        if self.instance._original_ad_domainname != self.instance.ad_domainname:
            return True
        if self.instance._original_ad_netbiosname != self.instance.ad_netbiosname:
            return True
        if self.instance._original_ad_allow_trusted_doms != self.instance.ad_allow_trusted_doms:
            return True
        if self.instance._original_ad_use_default_domain != self.instance.ad_use_default_domain:
            return True
        if self.instance._original_ad_unix_extensions != self.instance.ad_unix_extensions:
            return True
        if self.instance._original_ad_verbose_logging != self.instance.ad_verbose_logging:
            return True
        if self.instance._original_ad_bindname != self.instance.ad_bindname:
            return True
        if self.instance._original_ad_bindpw != self.instance.ad_bindpw:
            return True
        if self.instance._original_ad_keytab != self.instance.ad_keytab:
            return True
        if self.instance._original_ad_use_keytab != self.instance.ad_use_keytab:
            return True
        return False

    def __init__(self, *args, **kwargs):
        super(ActiveDirectoryForm, self).__init__(*args, **kwargs)
        if self.instance.ad_bindpw:
            self.fields['ad_bindpw'].required = False
        self.__original_save()

        self.fields["ad_enable"].widget.attrs["onChange"] = (
            "ad_mutex_toggle();"
        )

    def clean_ad_keytab(self):
        filename = "/data/krb5.keytab"

        ad_keytab = self.cleaned_data.get("ad_keytab", None)
        if ad_keytab and ad_keytab != filename:
            if hasattr(ad_keytab, 'temporary_file_path'):
                shutil.move(ad_keytab.temporary_file_path(), filename)
            else:
                with open(filename, 'wb+') as f:
                    for c in ad_keytab.chunks():
                        f.write(c)
                    f.close()

            os.chmod(filename, 0400)
            self.instance.ad_keytab = filename

        return filename

    def clean_ad_certfile(self):
        filename = "/data/activedirectory_certfile"

        ad_certfile = self.cleaned_data.get("ad_certfile", None)
        if ad_certfile and ad_certfile != filename:  
            if hasattr(ad_certfile, 'temporary_file_path'):
                shutil.move(ad_certfile.temporary_file_path(), filename)
            else:
                with open(filename, 'wb+') as f:
                    for c in ad_certfile.chunks():
                        f.write(c)
                    f.close()

            os.chmod(filename, 0400)
            self.instance.ad_certfile = filename

        return filename

    def clean(self):
        cdata = self.cleaned_data
        if not cdata.get("ad_bindpw"):
            cdata['ad_bindpw'] = self.instance.ad_bindpw

        if self.instance.ad_use_keytab == False:
            bindname = cdata.get("ad_bindname")
            bindpw = cdata.get("ad_bindpw")
            domain = cdata.get("ad_domainname")
            binddn = "%s@%s" % (bindname, domain)

            ret = FreeNAS_ActiveDirectory.validate_credentials(
                domain, binddn=binddn, bindpw=bindpw
            )
            if ret == False:
                raise forms.ValidationError(
                    _("Incorrect password.")
                )

        return cdata

    def save(self):
        enable = self.cleaned_data.get("ad_enable")
        if self.__original_changed():
            notifier()._clear_activedirectory_config()

        started = notifier().started("activedirectory")
        super(ActiveDirectoryForm, self).save()

        if enable:
            if started == True:
                started = notifier().restart("activedirectory")
            if started == False:
                started = notifier().start("activedirectory")
            if started == False:
                self.instance.ad_enable = False
                super(ActiveDirectoryForm, self).save()
                raise ServiceFailed("activedirectory",
                    _("Active Directory failed to reload."))
        else:
            if started == True:
                started = notifier().stop("activedirectory")


class NISForm(ModelForm):
    class Meta:
        fields = '__all__'
        model = models.NIS

    def __init__(self, *args, **kwargs):
        super(NISForm, self).__init__(*args, **kwargs)
        self.fields["nis_enable"].widget.attrs["onChange"] = (
            "nis_mutex_toggle();"
        )


class LDAPForm(ModelForm):
    ldap_certfile = FileField(label=_("Certificate"), required=False)

    class Meta:
        fields = '__all__'
        model = models.LDAP
        widgets = {
            'ldap_bindpw': forms.widgets.PasswordInput(render_value=True),
        }

    def __init__(self, *args, **kwargs):
        super(LDAPForm, self).__init__(*args, **kwargs)
        self.fields["ldap_enable"].widget.attrs["onChange"] = (
            "ldap_mutex_toggle();"
        )

    def clean_ldap_certfile(self):
        filename = "/data/ldap_certfile"

        ldap_certfile = self.cleaned_data.get("ldap_certfile", None)
        if ldap_certfile and ldap_certfile != filename:  
            if hasattr(ldap_certfile, 'temporary_file_path'):
                shutil.move(ldap_certfile.temporary_file_path(), filename)
            else:
                with open(filename, 'wb+') as f:
                    for c in ldap_certfile.chunks():
                        f.write(c)
                    f.close()

            os.chmod(filename, 0400)
            self.instance.ldap_certfile = filename

        return filename

    def clean(self):
        cdata = self.cleaned_data
        if not cdata.get("ldap_bindpw"):
            cdata["ldap_bindpw"] = self.instance.ldap_bindpw

        binddn = cdata.get("ldap_binddn")
        bindpw = cdata.get("ldap_bindpw")
        hostname = cdata.get("ldap_hostname")

        ret = FreeNAS_LDAP.validate_credentials(
            hostname, binddn=binddn, bindpw=bindpw
        )
        if ret == False:
            raise forms.ValidationError(
                _("Incorrect password.")
            )

        return cdata

    def save(self):
        enable = self.cleaned_data.get("ldap_enable")

        started = notifier().started("ldap")
        super(LDAPForm, self).save()

        if enable:
            if started == True:
                started = notifier().restart("ldap")
            if started == False:
                started = notifier().start("ldap")
            if started == False:
                self.instance.ad_enable = False
                super(LDAPForm, self).save()
                raise ServiceFailed("ldap",
                    _("LDAP failed to reload."))
        else:
            if started == True:
                started = notifier().stop("ldap")
