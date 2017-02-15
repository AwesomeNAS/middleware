<%
    from bsd import sysctl
    config = dispatcher.call_sync('service.snmp.get_config')

    if config['v3_username'] and config['v3_password']:
        v3_user = [config['v3_username'], config['v3_auth_type'], config['v3_password']]
        if config['v3_privacy_passphrase']:
            v3_user.append(config['v3_privacy_protocol'])
            v3_user.append(config['v3_privacy_passphrase'])
        v3_user = ' '.join(v3_user)

%>\
agentAddress udp:161,udp6:161,unix:/var/run/snmpd.sock
sysLocation ${config['location'] or 'unknown'}
sysContact ${config['contact'] or 'unknown@localhost'}
sysDescr Hardware: ${sysctl.sysctlbyname('hw.machine')} ${sysctl.sysctlbyname('hw.model')} running at ${sysctl.sysctlbyname('hw.clockrate')} Software: ${sysctl.sysctlbyname('kern.ostype')} ${sysctl.sysctlbyname('kern.osrelease')} (revision ${sysctl.sysctlbyname('kern.osrevision')})

##pass .1.3.6.1.4.1.25359.1 /usr/local/bin/freenas-snmp/zfs-snmp

%if config['v3']:
% if config['v3_username'] and config['v3_password']:
createUser ${v3_user}

rwuser ${config['v3_username']}
% endif
%else:
rocommunity ${config['community'] or 'community'} default
% endif