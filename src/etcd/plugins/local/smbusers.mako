% for user in ds.query('users', ('email', '!=', None)):
    ${user['username']} = ${user['smbhash']}
% endfor