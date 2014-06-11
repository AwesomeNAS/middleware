#!/usr/local/bin/python

import os
import sys

sys.path.extend([
    '/usr/local/www',
    '/usr/local/www/freenasUI'
])

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'freenasUI.settings')

# Make sure to load all modules
from django.db.models.loading import cache
cache.get_apps()


def main():
    """Use the django ORM to generate a config file.  We'll build the
    config file as a series of lines, and once that is done write it
    out in one go"""

    ctl_config = "/etc/ctl.conf"
    cf_contents = []

    from freenasUI.services.models import iSCSITargetGlobalConfiguration
    from freenasUI.services.models import iSCSITargetExtent
    from freenasUI.services.models import iSCSITargetPortal
    from freenasUI.services.models import iSCSITargetPortalIP
    from freenasUI.services.models import iSCSITargetAuthCredential
    from freenasUI.services.models import iSCSITarget
    from freenasUI.services.models import iSCSITargetToExtent

    # Generate the auth section
    # Work around SQLite not supporting DISTINCT ON
    val = None
    isopen = False
    AUTH = None
    for id in iSCSITargetAuthCredential.objects.order_by('iscsi_target_auth_tag'):
        if not val:
            val = id.iscsi_target_auth_tag
            isopen = True
        else:
            if val == id.iscsi_target_auth_tag:
                pass
            else:
                val = id.iscsi_target_auth_tag
                isopen = True
                AUTH = None
                cf_contents.append("}\n\n")
        if isopen:
            cf_contents.append("auth-group ag%d {\n" % id.iscsi_target_auth_tag)
            isopen = False
        # It is an error to mix CHAP and Mutual CHAP in the same auth group
        # But not in istgt, so we need to catch this and do something.
        # For now just skip over doing something that would cause ctld to bomb
        if id.iscsi_target_auth_peeruser and AUTH != "CHAP":
            AUTH = "Mutual"
            cf_contents.append("\tchap-mutual %s %s %s %s\n" % (id.iscsi_target_auth_user,
                                                                id.iscsi_target_auth_secret,
                                                                id.iscsi_target_auth_peeruser,
                                                                id.iscsi_target_auth_peersecret))
        elif AUTH != "Mutual":
            AUTH = "CHAP"
            cf_contents.append("\tchap %s %s\n" % (id.iscsi_target_auth_user,
                                                   id.iscsi_target_auth_secret))
    cf_contents.append("}\n\n")

    # Generate the portal-group section
    for portal in iSCSITargetPortal.objects.all():
        cf_contents.append("portal-group pg%s {\n" % portal.id)
        disc_authmethod = iSCSITargetGlobalConfiguration.objects.all()[0].iscsi_discoveryauthmethod
        if disc_authmethod == "None":
            cf_contents.append("\tdiscovery-auth-group no-authentication\n")
        else:
            cf_contents.append("\tdiscovery-auth-group %s\n" %
                               iSCSITargetGlobalConfiguration.objects.all()[0].iscsi_discoveryauthgroup)
        listen = iSCSITargetPortalIP.objects.filter(id=portal.id)
        for obj in listen:
            cf_contents.append("\tlisten %s:%s\n" % (obj.iscsi_target_portalip_ip,
                                                     obj.iscsi_target_portalip_port))
        cf_contents.append("}\n\n")

    # Generate the target section
    target_basename = iSCSITargetGlobalConfiguration.objects.all()[0].iscsi_basename
    for target in iSCSITarget.objects.all():
        cf_contents.append("target %s:%s {\n" % (target_basename, target.iscsi_target_name))
        if target.iscsi_target_name:
            cf_contents.append("\talias %s\n" % target.iscsi_target_name)
        if target.iscsi_target_authtype == "None" or target.iscsi_target_authtype == "Auto":
            cf_contents.append("\tauth-group no-authentication\n")
        else:
            cf_contents.append("\tauth-group ag%s\n" % target.iscsi_target_authgroup)
        cf_contents.append("\tportal-group pg%s\n" % target.iscsi_target_portalgroup)
        used_lunids = [
            o.iscsi_lunid
            for o in target.iscsitargettoextent_set.all().exclude(
                iscsi_lunid=None,
            )
        ]
        cur_lunid = 0
        for t2e in target.iscsitargettoextent_set.all().extra({
            'null_first': 'iscsi_lunid IS NULL',
        }).order_by('null_first', 'iscsi_lunid'):

            cf_contents.append("\t\t\n")
            if t2e.iscsi_lunid is None:
                while cur_lunid in used_lunids:
                    cur_lunid += 1
                cf_contents.append("\t\tlun %s {\n" % cur_lunid)
                cur_lunid += 1
            else:
                cf_contents.append("\t\tlun %s {\n" % t2e.iscsi_lunid)
            path = t2e.iscsi_extent.iscsi_target_extent_path
            size = t2e.iscsi_extent.iscsi_target_extent_filesize
            if not path.startswith("/mnt"):
                path = "/dev/" + path
            cf_contents.append("\t\t\tpath %s\n" % path)
            cf_contents.append("\t\t\tblocksize %s\n" % target.iscsi_target_logical_blocksize)
            cf_contents.append("\t\t\tserial %s\n" % target.iscsi_target_serial)
            cf_contents.append('\t\t\tdevice-id "FreeBSD iSCSI Disk"\n')
            if size != "0":
                if size.endswith('B'):
                    size = size.strip('B')
                cf_contents.append("\t\t\tsize %s\n" % size)
            cf_contents.append("\t\t}\n")
        cf_contents.append("}\n\n")

    fh = open(ctl_config, "w")
    for line in cf_contents:
        fh.write(line)
    fh.close()
    os.chmod(ctl_config, 0600)

if __name__ == "__main__":
    main()
