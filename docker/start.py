#!/usr/bin/env python3
"""Start Sofa Link in its container: check the settings, link the BIOS, write the relay files.

The compose file sets the environment. This script:
- refuses to start without a room code and an admin PIN that are long enough;
- links each BIOS dump from the read-only /bios mount into the writable system
  folder, because the PS2 core writes its own files next to the BIOS;
- writes the TURN relay's configuration and the browsers' ICE config to the
  relay volume. It makes the relay password once and keeps it;
- starts server.py. The PINs reach it in the environment, not on its command line.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
MIN_PIN = 8  # the room is on the internet: longer than the host's own minimum of 6
MIN_ADMIN_PIN = 6
HOST_NAME = re.compile(r'(?=.{1,253}\Z)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*')
WRITTEN = ('.nvm', '.mec')  # PS2 console settings and clock, next to the BIOS
RELAY_PORT = 3478  # inside the container; the compose file publishes it


class Refused(Exception):
    pass


def setting(env, name, default=''):
    return env.get(name, default).strip()


def port(env, name, default):
    value = setting(env, name) or str(default)
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise Refused(f'{name} must be a port from 1 to 65535')
    return int(value)


def address(env, name):
    value = setting(env, name)
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise Refused(f'{name} must be an IPv4 address; the compose file sets it') from None


def host_name(env, name):
    value = setting(env, name)
    if not HOST_NAME.fullmatch(value) or value.replace('.', '').isdigit() and not is_ip(value):
        raise Refused(f'{name} must be a host name or an IPv4 address: {value!r}')
    return value


def is_ip(value):
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def check_pins(env):
    pin, admin = env.get('SOFA_PIN', ''), env.get('SOFA_ADMIN_PIN', '')
    if len(pin) < MIN_PIN or pin != pin.strip():
        raise Refused(f'Set SOFA_PIN to a room code of at least {MIN_PIN} characters, with no spaces at the ends')
    if len(admin) < MIN_ADMIN_PIN or admin != admin.strip():
        raise Refused(f'Set SOFA_ADMIN_PIN to at least {MIN_ADMIN_PIN} characters, with no spaces at the ends')
    if admin == pin:
        raise Refused('SOFA_ADMIN_PIN must differ from SOFA_PIN: every player knows the room code')


def link_bios(bios, system):
    """Link every file under `bios` into `system` at the same path. Remove links to files that are gone."""
    bios, system = Path(bios), Path(system)
    system.mkdir(parents=True, exist_ok=True)
    for link in system.rglob('*'):
        if link.is_symlink() and Path(os.readlink(link)).is_relative_to(bios) and not link.exists():
            link.unlink()
    count = 0
    if not bios.is_dir():
        return count
    for path in sorted(bios.rglob('*')):
        if not path.is_file() or any(part.startswith('.') for part in path.relative_to(bios).parts):
            continue
        link = system/path.relative_to(bios)
        link.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() in WRITTEN:
            # The core writes these: copy them once, then they are the core's own.
            if link.is_symlink():
                link.unlink()
            if not link.exists():
                shutil.copyfile(path, link)
            continue
        if link.is_symlink() and os.readlink(link) == str(path):
            count += 1
            continue
        if link.is_symlink() or not link.exists():
            link.unlink(missing_ok=True)
            link.symlink_to(path)
            count += 1
        # A real file of the same name is the core's own; keep it.
    return count


def password(turn, new=False):
    """The relay password: made once, kept in the relay volume."""
    path = Path(turn)/'password'
    if not new and path.is_file():
        value = path.read_text().strip()
        if re.fullmatch(r'[0-9a-f]{32,}', value):
            return value
    value = secrets.token_hex(24)
    temporary = path.with_suffix('.new')
    temporary.write_text(value+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)
    return value


def relay_files(env, turn):
    """Write turnserver.conf and ice.json for the relay service.

    Returns the ICE config path, or None when neither SOFA_TURN_PUBLIC_HOST nor
    SOFA_TURN_LAN_HOST is set. The relay configuration is written either way,
    so the relay service runs, and browsers are offered it once a name is set.
    """
    relay_ip, host_ip = address(env, 'SOFA_RELAY_IP'), address(env, 'SOFA_HOST_IP')
    urls = []
    if setting(env, 'SOFA_TURN_PUBLIC_HOST'):
        # The relay's TLS is the reverse proxy's job; browsers use turns: on the public name.
        urls.append(f"turns:{host_name(env, 'SOFA_TURN_PUBLIC_HOST')}:{port(env, 'SOFA_TURN_PUBLIC_PORT', 5349)}"
                    '?transport=tcp')
    if setting(env, 'SOFA_TURN_LAN_HOST'):
        # Optional: LAN browsers can reach the published plain-TCP port directly.
        urls.append(f"turn:{host_name(env, 'SOFA_TURN_LAN_HOST')}:{port(env, 'SOFA_TURN_PORT', RELAY_PORT)}"
                    '?transport=tcp')
    turn = Path(turn)
    turn.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = password(turn, setting(env, 'SOFA_TURN_NEW_PASSWORD') in ('1', 'true', 'yes'))
    config = f"""\
# Made by docker/start.py each time the game host starts. Do not edit.
listening-ip={relay_ip}
listening-port={RELAY_PORT}
relay-ip={relay_ip}
realm=sofa-link
lt-cred-mech
user=sofa:{secret}
fingerprint
# Browsers use TCP only. The reverse proxy gives TLS.
no-udp
no-tls
no-dtls
no-tcp-relay
# Relay only to the game host on the compose network. coturn always refuses loopback peers.
allowed-peer-ip={host_ip}
denied-peer-ip=0.0.0.0-255.255.255.255
denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
no-multicast-peers
# Twelve browsers with two relay addresses each.
total-quota=24
min-port=49152
max-port=49407
no-cli
no-software-attribute
pidfile=/tmp/turnserver.pid
userdb=/tmp/turndb
log-file=stdout
simple-log
"""
    ice = [dict(urls=urls, username='sofa', credential=secret)] if urls else []
    for name, text in (('turnserver.conf', config), ('ice.json', json.dumps(ice, indent=2)+'\n')):
        temporary = turn/(name+'.new')
        temporary.write_text(text)
        temporary.chmod(0o600)
        temporary.replace(turn/name)
    if not urls:
        return None
    print('Relay: ' + ' and '.join(urls), flush=True)
    return turn/'ice.json'


def command(env, ice):
    args = [sys.executable, str(ROOT/'server.py'), '--library', setting(env, 'SOFA_LIBRARY', '/games'),
            '--system', setting(env, 'SOFA_SYSTEM', '/data/system'), '--saves', setting(env, 'SOFA_SAVES', '/data/saves'),
            '--covers', setting(env, 'SOFA_COVERS', '/data/covers'),
            '--host', '0.0.0.0', '--port', '8081']
    if setting(env, 'SOFA_TRUSTED_PROXY'):
        args += ['--trusted-proxy', setting(env, 'SOFA_TRUSTED_PROXY')]
    if ice:
        args += ['--ice-config', str(ice)]
    if setting(env, 'SOFA_BOOT_SCREEN', 'yes') in ('0', 'no', 'false'):
        args.append('--no-boot-screen')
    return args


def main(env=os.environ):
    os.umask(0o077)
    try:
        check_pins(env)
        library = Path(setting(env, 'SOFA_LIBRARY', '/games'))
        if not library.is_dir():
            raise Refused(f'No game library at {library}: mount it, with one subfolder per console')
        linked = link_bios(setting(env, 'SOFA_BIOS', '/bios'), setting(env, 'SOFA_SYSTEM', '/data/system'))
        Path(setting(env, 'SOFA_SAVES', '/data/saves')).mkdir(parents=True, exist_ok=True)
        Path(setting(env, 'SOFA_COVERS', '/data/covers')).mkdir(parents=True, exist_ok=True)
        ice = relay_files(env, setting(env, 'SOFA_TURN_STATE', '/turn'))
        args = command(env, ice)
    except Refused as error:
        print(f'Sofa Link: {error}', file=sys.stderr, flush=True)
        return 2
    print(f'BIOS files linked: {linked}' + ('' if ice else
          '. Browsers get no relay: set SOFA_TURN_PUBLIC_HOST (or SOFA_TURN_LAN_HOST)'), flush=True)
    os.execv(args[0], args)


if __name__ == '__main__':
    sys.exit(main())
