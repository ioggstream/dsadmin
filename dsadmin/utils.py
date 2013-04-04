"""Utilities for DSAdmin.

    TODO put them in a module!
"""
try:
    from subprocess import Popen as my_popen, PIPE
except ImportError:
    from popen2 import popen2

    def my_popen(cmd_l, stdout=None):
        class MockPopenResult(object):
            def wait():
                pass
        p = MockPopenResult()
        p.stdout, p.stdin = popen2(cmd_l)
        return p


import dsadmin

import socket
from socket import getfqdn

import ldap
import re
import os
import logging
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


#
# Decorator
#


def static_var(varname, value):
    def decorate(func):
        setattr(func, varname, value)
        return func
    return decorate

#
# constants
#
DEFAULT_USER = "nobody"
#
# Various searchs to be used in getEntry
#   eg getEntry(*searchs['NAMINGCONTEXTS'])
#
searchs = {
    'NAMINGCONTEXTS': ('', ldap.SCOPE_BASE, '(objectclass=*)', ['namingcontexts'])
}

#
# Utilities
#


def is_a_dn(dn):
    """Returns True if the given string is a DN, False otherwise."""
    return (dn.find("=") > 0)


def normalizeDN(dn, usespace=False):
    # not great, but will do until we use a newer version of python-ldap
    # that has DN utilities
    ary = ldap.explode_dn(dn.lower())
    joinstr = ","
    if usespace:
        joinstr = ", "
    return joinstr.join(ary)


def escapeDNValue(dn):
    '''convert special characters in a DN into LDAPv3 escapes.

     e.g.
    "dc=example,dc=com" -> \"dc\=example\,\ dc\=com\"'''
    for cc in (' ', '"', '+', ',', ';', '<', '>', '='):
        dn = dn.replace(cc, '\\' + cc)
    return dn


def escapeDNFiltValue(dn):
    '''convert special characters in a DN into LDAPv3 escapes
    for use in search filters'''
    for cc in (' ', '"', '+', ',', ';', '<', '>', '='):
        dn = dn.replace(cc, '\\%x' % ord(cc))
    return dn


def suffixfilt(suffix):
    """Return a filter matching any possible suffix form.

        eg. normalized, escaped, spaced...
    """
    nsuffix = normalizeDN(suffix)
    spacesuffix = normalizeDN(nsuffix, True)
    escapesuffix = escapeDNFiltValue(nsuffix)
    filt = '(|(cn=%s)(cn=%s)(cn=%s)(cn="%s")(cn="%s")(cn=%s)(cn="%s"))' % (escapesuffix, nsuffix, spacesuffix, nsuffix, spacesuffix, suffix, suffix)
    return filt

#
# path tools
#


def get_sbin_dir(sroot, prefix):
    if sroot:
        return "%s/bin/slapd/admin/bin" % sroot
    elif prefix:
        return "%s/sbin" % prefix
    return "/usr/sbin"


#
# functions using sockets
#
def isLocalHost(host_name):
    """True if host_name points to a local ip.

        Uses gethostbyname()
    """
    # first see if this is a "well known" local hostname
    if host_name == 'localhost' or host_name == 'localhost.localdomain':
        return True

    # first lookup ip addr
    try:
        ip_addr = socket.gethostbyname(host_name)
        if ip_addr.startswith("127."):
            log.debug("this ip is on loopback, retain only the first octet")
            ip_addr = '127.'
    except socket.gaierror:
        log.debug("no ip address for %r" % host_name)
        return False

    # next, see if this IP addr is one of our
    # local addresses
    p = my_popen(['/sbin/ifconfig', '-a'], stdout=PIPE)
    child_stdout = p.stdout.read()
    found = ('inet addr:' + ip_addr) in child_stdout
    p.wait()

    return found


def getdomainname(name=''):
    fqdn = getfqdn(name)
    index = fqdn.find('.')
    if index >= 0:
        return fqdn[index + 1:]
    else:
        return fqdn


def getdefaultsuffix(name=''):
    dm = getdomainname(name)
    if dm:
        return "dc=" + dm.replace('.', ',dc=')
    else:
        return 'dc=localdomain'


def get_server_user(args):
    """Return the username used from the server inspecting the following keys in args.

        'newuserid', 'admconf', 'sroot' -> ssusers.conf

    """
    if 'newuserid' not in args:
        if 'admconf' in args:
            args['newuserid'] = args['admconf'].SuiteSpotUserID
        elif 'sroot' in args:
            ssusers = open("%s/shared/config/ssusers.conf" % args['sroot'])
            for line in ssusers:
                ary = line.split()
                if len(ary) > 1 and ary[0] == 'SuiteSpotUser':
                    args['newuserid'] = ary[-1]
            ssusers.close()
    if 'newuserid' not in args:
        args['newuserid'] = os.environ['LOGNAME']
        if args['newuserid'] == 'root':
            args['newuserid'] = DEFAULT_USER


def update_newhost_with_fqdn(args):
    """Replace args['newhost'] with its fqdn and returns True if local.

    One of the arguments to createInstance is newhost.  If this is specified, we need
    to convert it to the fqdn.  If not given, we need to figure out what the fqdn of the
    local host is.  This method sets newhost in args to the appropriate value and
    returns True if newhost is the localhost, False otherwise"""
    if 'newhost' in args:
        args['newhost'] = getfqdn(args['newhost'])
        isLocal = isLocalHost(args['newhost'])
    else:
        isLocal = True
        args['newhost'] = getfqdn()
    return isLocal


def getcfgdsuserdn(cfgdn, args):
    """Return a connection to a server.

    If the config ds user ID was given, not the full DN, we need to figure
    out what the full DN is.  Try to search the directory anonymously first.  If
    that doesn't work, look in ldap.conf.  If that doesn't work, just try the
    default DN.  This may raise a file or LDAP exception.  Returns a DSAdmin
    object bound as either anonymous or the admin user."""
    # create a connection to the cfg ds
    conn = DSAdmin(args['cfgdshost'], args['cfgdsport'], "", "")
    # if the caller gave a password, but not the cfguser DN, look it up
    if 'cfgdspwd' in args and \
            ('cfgdsuser' not in args or not is_a_dn(args['cfgdsuser'])):
        if 'cfgdsuser' in args:
            ent = conn.getEntry(cfgdn, ldap.SCOPE_SUBTREE,
                                "(uid=%s)" % args['cfgdsuser'],
                                ['dn'])
            args['cfgdsuser'] = ent.dn
        elif 'sroot' in args:
            ldapconf = open(
                "%s/shared/config/ldap.conf" % args['sroot'], 'r')
            for line in ldapconf:
                ary = line.split()  # default split is all whitespace
                if len(ary) > 1 and ary[0] == 'admnm':
                    args['cfgdsuser'] = ary[-1]
            ldapconf.close()
        elif 'admconf' in args:
            args['cfgdsuser'] = args['admconf'].userdn
        elif 'cfgdsuser' in args:
            args['cfgdsuser'] = "uid=%s,ou=Administrators,ou=TopologyManagement,%s" % \
                (args['cfgdsuser'], cfgdn)
        conn.unbind()
        conn = DSAdmin(
            args['cfgdshost'], args['cfgdsport'], args['cfgdsuser'],
            args['cfgdspwd'])
    return conn


def update_admin_domain(isLocal, args):
    """Get the admin domain to use."""
    if isLocal and 'admin_domain' not in args:
        if 'admconf' in args:
            args['admin_domain'] = args['admconf'].admindomain
        elif 'sroot' in args:
            dsconf = open('%s/shared/config/ds.conf' % args['sroot'], 'r')
            for line in dsconf:
                ary = line.split(":")
                if len(ary) > 1 and ary[0] == 'AdminDomain':
                    args['admin_domain'] = ary[1].strip()
            dsconf.close()


def getoldcfgdsinfo(args):
    """Use the old style sroot/shared/config/dbswitch.conf to get the info"""
    dbswitch = open("%s/shared/config/dbswitch.conf" % args['sroot'], 'r')
    try:
        matcher = re.compile(r'^directory\s+default\s+')
        for line in dbswitch:
            m = matcher.match(line)
            if m:
                url = LDAPUrl(line[m.end():])
                ary = url.hostport.split(":")
                if len(ary) < 2:
                    ary.append(389)
                else:
                    ary[1] = int(ary[1])
                ary.append(url.dn)
                return ary
    finally:
        dbswitch.close()


def getnewcfgdsinfo(args):
    """Use the new style prefix/etc/dirsrv/admin-serv/adm.conf.

        args = {'admconf': obj } where obj.ldapurl != None
    """
    url = LDAPUrl(args['admconf'].ldapurl)
    ary = url.hostport.split(":")
    if len(ary) < 2:
        ary.append(389)
    else:
        ary[1] = int(ary[1])
    ary.append(url.dn)
    return ary


def getcfgdsinfo(args):
    """Returns a 3-tuple consisting of the host, port, and cfg suffix.

        `args` = {
            'cfgdshost':
            'cfgdsport':
            'new_style':
        }
    We need the host and port of the configuration directory server in order
    to create an instance.  If this was not given, read the dbswitch.conf file
    to get the information.  This method will raise an exception if the file
    was not found or could not be open.  This assumes args contains the sroot
    parameter for the server root path.  If successful, """
    try:
        return args['cfgdshost'], int(args['cfgdsport']), dsadmin.CFGSUFFIX
    except KeyError:  # if keys are missing...
        if args['new_style']:
            return getnewcfgdsinfo(args)

        return getoldcfgdsinfo(args)


def getserverroot(cfgconn, isLocal, args):
    """Grab the serverroot from the instance dir of the config ds if the user
    did not specify a server root directory"""
    if cfgconn and 'sroot' not in args and isLocal:
        ent = cfgconn.getEntry(
            DN_CONFIG, ldap.SCOPE_BASE, "(objectclass=*)",
            ['nsslapd-instancedir'])
        if ent:
            args['sroot'] = os.path.dirname(
                ent.getValue('nsslapd-instancedir'))


@staticmethod
def getadminport(cfgconn, cfgdn, args):
    """Return a 2-tuple (asport, True) if the admin server is using SSL, False otherwise.

    Get the admin server port so we can contact it via http.  We get this from
    the configuration entry using the CFGSUFFIX and cfgconn.  Also get any other
    information we may need from that entry.  The ."""
    asport = 0
    secure = False
    if cfgconn:
        dn = cfgdn
        if 'admin_domain' in args:
            dn = "cn=%s,ou=%s, %s" % (
                args['newhost'], args['admin_domain'], cfgdn)
        filt = "(&(objectclass=nsAdminServer)(serverHostName=%s)" % args[
            'newhost']
        if 'sroot' in args:
            filt += "(serverRoot=%s)" % args['sroot']
        filt += ")"
        ent = cfgconn.getEntry(
            dn, ldap.SCOPE_SUBTREE, filt, ['serverRoot'])
        if ent:
            if 'sroot' not in args and ent.serverRoot:
                args['sroot'] = ent.serverRoot
            if 'admin_domain' not in args:
                ary = ldap.explode_dn(ent.dn, 1)
                args['admin_domain'] = ary[-2]
            dn = "cn=configuration, " + ent.dn
            ent = cfgconn.getEntry(dn, ldap.SCOPE_BASE, '(objectclass=*)',
                                   ['nsServerPort', 'nsSuiteSpotUser', 'nsServerSecurity'])
            if ent:
                asport = ent.nsServerPort
                secure = (ent.nsServerSecurity and (
                    ent.nsServerSecurity == 'on'))
                if 'newuserid' not in args:
                    args['newuserid'] = ent.nsSuiteSpotUser
        cfgconn.unbind()
    return asport, secure


def formatInfData(args):
    """Format args data for input to setup or migrate taking inf style data.

        args = {
            newhost, newuserid, newport, newrootdn, newrootpw, newsuffix,
            have_admin, cfgdshost, cfgdsport, cfgdsuser,cfgdspwd,
            InstallLdifFile, AddOrgEntries, ConfigFile, SchemaFile, ldapifilepath
        'have_admin',
    """
    args = args.copy()
    args['CFGSUFFIX'] = dsadmin.CFGSUFFIX

    content = """[General]
FullMachineName= %(newhost)s
SuiteSpotUserID= %(newuserid)s
""" % args

    if args['have_admin']:
        content += """
ConfigDirectoryLdapURL= ldap://%(cfgdshost)s:%(cfgdsport)d/%(CFGSUFFIX)s
ConfigDirectoryAdminID= %(cfgdsuser)s
ConfigDirectoryAdminPwd= %(cfgdspwd)s
AdminDomain= %(admin_domain)s
""" % args

    content += """\n\n
[slapd]
ServerPort= %(newport)s
RootDN= %(newrootdn)s
RootDNPwd= %(newrootpw)s
ServerIdentifier= %(newinst)s
Suffix= %(newsuffix)s\n""" % args

    if 'InstallLdifFile' in args:
        content += """\nInstallLdifFile= %s\n""" % args['InstallLdifFile']
    if 'AddOrgEntries' in args:
        content += """\nAddOrgEntries= %s\n""" % args['AddOrgEntries']
    if 'ConfigFile' in args:
        for ff in args['ConfigFile']:
            content += """\nConfigFile= %s\n""" % ff
    if 'SchemaFile' in args:
        for ff in args['SchemaFile']:
            content += """\nSchemaFile= %s\n""" % ff

    if 'ldapifilepath' in args:
        content += "\nldapifilepath=%s\n" % args['ldapifilepath']

    return content