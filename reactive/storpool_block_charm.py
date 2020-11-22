"""
A Juju charm that installs the StorPool block (client initiator) service and
provides the configuration needed for other StorPool charms.

Subordinate hooks to the master:
- juju-info: attach to another charm (e.g. `nova-compute` or `cinder-storpool`)
  as a subordinate

Configuration hooks:
- storpool-presence: announce to other charms when the StorPool client service
  has been installed and configured on the Juju nodes

Internal hooks:
- block-p: announce to the other `storpool-block` units that this unit has
  been installed and configured
"""

from __future__ import print_function

import json
import pathlib
import platform
import re
import subprocess
import tempfile

from charms import reactive
from charmhelpers.core import hookenv, host, unitdata

from spcharms import error as sperror
from spcharms import kvdata
from spcharms import osi
from spcharms import service_hook
from spcharms import status as spstatus
from spcharms import utils as sputils

from spcharms.run import storpool_block as run_block


RELATIONS = ["block-p", "storpool-presence"]

RE_SPDEV = re.compile(
    r"""
    ^
    /dev/sp-
    (?: 0 | [1-9][0-9]* )
    $""",
    re.X,
)

BLOCK_CONFFILE = pathlib.Path(
    "/etc/storpool.conf.d/storpool-cinder-block.conf"
)

STORPOOL_CONFFILE = pathlib.Path("/etc/storpool.conf")


def rdebug(s, cond=None):
    """
    Pass the diagnostic message string `s` to the central diagnostic logger.
    """
    sputils.rdebug(s, prefix="block-charm", cond=cond)


@reactive.hook("install")
def install_setup():
    """
    Note that the storpool-repo-add layer should reset the status error
    messages on "config-changed" and "upgrade-charm" hooks.
    """
    spstatus.set_status_reset_handler("storpool-repo-add")
    run()


@reactive.hook("config-changed")
def config_changed():
    """
    Try to (re-)install everything and re-announce.
    """
    if reactive.is_state("storpool-block-charm.leader"):
        reactive.set_state("storpool-block-charm.bump-generation")
    run()


@reactive.hook("upgrade-charm")
def upgrade_setup():
    """
    Try to (re-)install everything and re-announce.
    """
    run()


@reactive.hook("start")
def start_service():
    """
    Try to (re-)install everything.
    """
    run()


@reactive.hook("post-series-upgrade")
def post_series_upgrade():
    """ Try to upgrade and start everything. """
    run()


@reactive.hook("leader-elected")
def we_are_the_leader():
    """
    Make note of the fact that this unit has been elected as the leader for
    the `storpool-block` charm.  This will prompt the unit to send presence
    information to the other charms along the `storpool-presence` hook.
    """
    rdebug("have we really been elected leader?")
    try:
        hookenv.leader_set(charm_storpool_block_unit=sputils.get_machine_id())
    except Exception as e:
        rdebug("no, could not run leader_set: {e}".format(e=e))
        reactive.remove_state("storpool-block-charm.leader")
        return
    rdebug("looks like we have been elected leader")
    reactive.set_state("storpool-block-charm.leader")


@reactive.hook("leader-settings-changed")
def we_are_not_the_leader():
    """
    Make note of the fact that this unit is no longer the leader for
    the `storpool-block` charm, so no longer attempt to send presence data.
    """
    rdebug("welp, we are not the leader")
    reactive.remove_state("storpool-block-charm.leader")


@reactive.hook("leader-deposed")
def we_are_no_longer_the_leader():
    """
    Make note of the fact that this unit is no longer the leader for
    the `storpool-block` charm, so no longer attempt to send presence data.
    """
    rdebug("welp, we have been deposed as leader")
    reactive.remove_state("storpool-block-charm.leader")


@reactive.when("storpool-block-charm.services-started")
@reactive.when("block-p.notify")
def peers_changed(_):
    try_announce()
    update_status()


@reactive.when("storpool-block-charm.services-started")
@reactive.when("storpool-presence.notify")
def cinder_changed(_):
    try_announce()
    update_status()


def try_announce():
    try:
        announce_presence()
        # Only reset the flag afterwards so that if anything
        # goes wrong we can retry this later
        # It should be safe to reset both states at once; we always
        # fetch data on both hooks, so we can't miss anything.
        reactive.remove_state("block-p.notify")
        reactive.remove_state("block-p.notify-joined")
        reactive.remove_state("storpool-presence.notify")
        reactive.remove_state("storpool-presence.notify-joined")
    except Exception:  # FIXME: as err:
        raise  # FIXME: hookenv.log and then exit(42)?


def read_storpool_conf():
    try:
        return STORPOOL_CONFFILE.read_text(encoding="UTF-8")
    except FileNotFoundError:
        return None


def announce_presence(force=False):
    data = service_hook.fetch_presence(RELATIONS)

    mach_id = "block:" + sputils.get_machine_id()

    announce = force
    block_joined = reactive.is_state("block-p.notify-joined")
    cinder_joined = reactive.is_state("storpool-presence.notify-joined")
    if cinder_joined or block_joined:
        announce = True

    generation = int(data["generation"])
    if generation < 0:
        generation = 0

    if reactive.is_state("storpool-block-charm.bump-generation"):
        generation = generation + 1
        announce = True

    if announce:
        our_node = {"generation": generation, "hostname": platform.node()}
        ndata = {"generation": generation, "nodes": {mach_id: our_node}}
        rdebug("announcing {data}".format(data=ndata), cond="announce")
        service_hook.send_presence(ndata, RELATIONS)

    reactive.remove_state("storpool-block-charm.bump-generation")

    check_for_new_presence(data)


def check_for_new_presence(data):
    found = None
    old = unitdata.kv().get(kvdata.KEY_LXD_NAME)
    our_mach_id = sputils.get_machine_id()

    for node in data["nodes"]:
        if not node.startswith("cinder:"):
            continue
        mach_id = node[7:]
        parts = mach_id.split("/")
        if len(parts) == 3 and parts[1] == "lxd":
            if parts[0] == our_mach_id:
                rdebug(
                    "found our container: {lx}".format(lx=mach_id),
                    cond="announce",
                )
                if found is None:
                    found = mach_id
                    if old is None or old != mach_id:
                        rdebug(
                            "setting Cinder container {mach_id}".format(
                                mach_id=mach_id
                            )
                        )
                        unitdata.kv().set(kvdata.KEY_LXD_NAME, mach_id)
                        reactive.set_state("storpool-block-charm.lxd")

    if not found:
        rdebug("- no Cinder containers here", cond="announce")
        if old is not None:
            rdebug("forgetting about Cinder container {old}".format(old=old))
            unitdata.kv().set(kvdata.KEY_LXD_NAME, None)
            reactive.set_state("storpool-block-charm.lxd")


class BlockMirrorMigrate:
    """ Perform a nsenter-to-mirrordir migration. """

    def __init__(self, cinder_name):
        """ Initialize a migration object. """
        self.cinder_name = cinder_name
        self.clear_cache()

    def get_container_config(self):
        """ Get the LXC configuration of the container. """
        if self.container_config is not None:
            return self.container_config

        if self.cinder_name is None:
            return None

        needle = "-" + self.cinder_name.replace("/", "-")
        container = [
            item
            for item in json.loads(
                subprocess.check_output(
                    ["env", "LC_ALL=C", "lxc", "list", "--format=json"],
                    shell=False,
                ).decode("UTF-8")
            )
            if item["name"].endswith(needle)
        ]
        if not container:
            rdebug(
                "Could not find the {name} container running".format(
                    name=self.cinder_name
                )
            )
            return None
        elif len(container) != 1:
            rdebug(
                "Cannot handle more than one {name} container".format(
                    name=self.cinder_name
                )
            )
            return None

        self.container_config = container[0]
        return self.container_config

    def get_storpool_major(self):
        """ Get the major number for the StorPool block devices. """
        if self.storpool_major is not None:
            return self.storpool_major

        # "us-ascii" should be enough, but let us not take any chances
        with open("/proc/devices", mode="r", encoding="Latin-1") as devf:
            lines = [line.strip().split() for line in devf.readlines()]
        found = [
            item for item in lines if len(item) > 1 and item[1] == "StorPool"
        ]
        if not found:
            rdebug("Could not find a StorPool line in /proc/devices")
            return None
        if len(found) != 1:
            rdebug("Found more than one StorPool line in /proc/devices")
            return None

        self.storpool_major = int(found[0][0])
        return self.storpool_major

    def get_contained_devices(self):
        """ Find the devices in the container. """
        if self.contained_devices is not None:
            return self.contained_devices

        if self.container_config is None or self.storpool_major is None:
            return None
        major_hex = "{0:x}".format(self.storpool_major)

        outp = (
            subprocess.check_output(
                [
                    "env",
                    "LC_ALL=C",
                    "lxc",
                    "exec",
                    "--",
                    self.container_config["name"],
                    "find",
                    "/dev/",
                    "-mindepth",
                    "1",
                    "-maxdepth",
                    "1",
                    "-name",
                    "sp-*",
                    "-exec",
                    "stat",
                    "-c",
                    "%n\t%t\n",
                    "{}",
                    ";",
                ],
                shell=False,
            )
            .decode("Latin-1")
            .split("\n")
        )
        items = [line.split("\t") for line in outp]
        found = [
            item[0] for item in items if len(item) > 1 and item[1] == major_hex
        ]

        self.contained_devices = found
        return self.contained_devices

    def get_contained_mounts(self):
        """ Find the devices in the container. """
        if self.contained_mounts is not None:
            return self.contained_mounts

        if self.container_config is None:
            return None

        outp = (
            subprocess.check_output(
                [
                    "env",
                    "LC_ALL=C",
                    "lxc",
                    "exec",
                    "--",
                    self.container_config["name"],
                    "cat",
                    "/proc/mounts",
                ],
                shell=False,
            )
            .decode("Latin-1")
            .split("\n")
        )
        items = [line.split() for line in outp]
        found = [
            item[1]
            for item in items
            if len(item) > 2 and RE_SPDEV.match(item[1]) and item[2] == "tmpfs"
        ]

        self.contained_mounts = found
        return self.contained_mounts

    def get_storpool_mirror_dir(self):
        """ Find the directory where storpool_block mirrors the devices. """
        if self.storpool_mirror_dir is not None:
            return self.storpool_mirror_dir

        try:
            with open(
                "/run/storpool_block.bin.pid", mode="r", encoding="us-ascii"
            ) as pidf:
                pid = int(pidf.readlines()[0].strip())
        except OSError as err:
            rdebug(
                "Could not read the storpool_block pid file: "
                "{etype}: {err}".format(etype=type(err).__name__, err=err)
            )
            return None

        try:
            with open(
                "/proc/{pid}/cmdline".format(pid=pid),
                mode="r",
                encoding="Latin-1",
            ) as cmdf:
                data = cmdf.read().split("\0")
        except OSError as err:
            rdebug(
                "Could not read /proc/{pid}/cmdline: {etype}: {err}".format(
                    pid=pid, etype=type(err).__name__, err=err
                )
            )
            return None

        try:
            dirname = data[data.index("-M") + 1]
        except ValueError:
            rdebug("There is no StorPool mirror directory (no '-M' option)")
            return None

        self.storpool_mirror_dir = dirname
        return dirname

    def get_container_mirror_dir(self):
        """ Get the path where the StorPool mirror dir is mounted within. """
        if self.container_mirror_dir is not None:
            return self.container_mirror_dir

        if self.container_config is None or self.storpool_mirror_dir is None:
            return None

        found = [
            item
            for item in self.container_config["devices"].items()
            if item[1].get("source") == self.storpool_mirror_dir
        ]
        if not found:
            return None

        self.container_mirror_dir = found[0]
        return self.container_mirror_dir

    def clear_cache(self):
        """ Remove any cached detected data. """
        self.container_config = None
        self.storpool_mirror_dir = None
        self.storpool_major = None
        self.contained_devices = None
        self.contained_mounts = None
        self.container_mirror_dir = None

    def detect(self, force=False):
        """ Find out as much as we can about the environment. """
        if force:
            self.clear_cache()

        self.get_container_config()
        self.get_storpool_mirror_dir()
        self.get_container_mirror_dir()
        self.get_storpool_major()
        self.get_contained_devices()
        self.get_contained_mounts()

    def done(self):
        """ Is the migration complete? """
        return (
            self.storpool_mirror_dir is not None
            and self.container_mirror_dir is not None
            and not self.contained_mounts
            and not self.contained_devices
        )

    def unready(self):
        """ Are the prerequisites not met yet? """
        if self.storpool_mirror_dir is None:
            rdebug("no mirror directory support in storpool_block")
            return True

        if self.container_config is None:
            rdebug("no data about the Cinder container")
            return True

        return False

    def run(self):
        """ Attempt to perform the migration. """
        if self.unready():
            return False

        if self.container_mirror_dir is None:
            rdebug("Trying to add the StorPool mirror dir to the container")
            res = subprocess.call(
                [
                    "env",
                    "LC_ALL=C",
                    "lxc",
                    "config",
                    "device",
                    "add",
                    "--",
                    self.container_config["name"],
                    "dev-storpool",
                    "disk",
                    "path=/dev/storpool",
                    "source={mirror}".format(mirror=self.storpool_mirror_dir),
                ],
                shell=False,
            )
            if res != 0:
                rdebug(
                    "Could not add the StorPool mirror directory: "
                    "lxc exit code {res}".format(res=res)
                )
                return False

        if self.contained_mounts:
            errors = False
            for mount in self.contained_mounts:
                rdebug(
                    "Trying to unmount {mount} within the container".format(
                        mount=mount
                    )
                )
                res = subprocess.call(
                    [
                        "env",
                        "LC_ALL=C",
                        "lxc",
                        "exec",
                        "--",
                        self.container_config["name"],
                        "umount",
                        "--",
                        mount,
                    ],
                    shell=False,
                )
                if res != 0:
                    rdebug(
                        "Could not unmount {mount}: "
                        "lxc exit code {res}".format(mount=mount, res=res)
                    )
                    errors = True
            if errors:
                return False

        if self.contained_devices:
            rdebug(
                "Trying to remove devices from the container: {devs}".format(
                    devs=" ".join(self.contained_devices)
                )
            )
            res = subprocess.call(
                [
                    "env",
                    "LC_ALL=C",
                    "lxc",
                    "exec",
                    "--",
                    self.container_config["name"],
                    "rm",
                    "--",
                ]
                + list(self.contained_devices),
                shell=False,
            )
            if res != 0:
                rdebug(
                    "Could not remove the devices: "
                    "lxc exit code {res}".format(res=res)
                )
                return False

        rdebug("The migration seems to be complete!")
        return True

    def __str__(self):
        """ Provide a human-readable representation. """
        return (
            "{otype}("
            "cinder_name {cinder_name}"
            " container_config {container_config}"
            " storpool_mirror_dir {storpool_mirror_dir}"
            " container_mirror_dir {container_mirror_dir}"
            " storpool_major {storpool_major}"
            " contained_devices {contained_devices}"
            " contained_mounts {contained_mounts}"
            ")".format(
                otype=type(self).__name__,
                cinder_name=repr(self.cinder_name),
                container_config=repr(
                    {
                        "name": self.container_config["name"],
                        "devices": self.container_config["devices"],
                    }
                ),
                storpool_mirror_dir=repr(self.storpool_mirror_dir),
                contained_devices=repr(self.contained_devices),
                contained_mounts=repr(self.contained_mounts),
                storpool_major=repr(self.storpool_major),
                container_mirror_dir=repr(self.container_mirror_dir),
            )
        )


def remove_block_conffile(confname):
    """
    Remove a previously-created storpool_block config file that
    instructs it to expose devices to LXD containers.
    """
    rdebug(
        "no Cinder LXD containers found, checking for "
        "any previously stored configuration..."
    )
    removed = False
    if confname.is_file():
        rdebug(
            "- yes, {confname} exists, removing it".format(confname=confname)
        )
        try:
            confname.unlink()
            removed = True
        except Exception as e:
            rdebug(
                "could not remove {confname}: {e}".format(
                    confname=confname, e=e
                )
            )
    elif confname.exists():
        rdebug(
            "- well, {confname} exists, but it is not a file; "
            "removing it anyway".format(confname=confname)
        )
        subprocess.call(["rm", "-rf", "--", str(confname)])
        removed = True
    if removed:
        rdebug(
            "- let us try to restart the storpool_block service "
            + "(it may not even have been started yet, so ignore errors)"
        )
        try:
            if host.service_running("storpool_block"):
                rdebug(
                    "  - well, it does seem to be running, so "
                    + "restarting it"
                )
                host.service_restart("storpool_block")
            else:
                rdebug("  - nah, it was not running at all indeed")
        except Exception as e:
            rdebug(
                "  - could not restart the service, but "
                "ignoring the error: {e}".format(e=e)
            )


def create_block_conffile(lxc_name, confname):
    """
    Create the storpool_block config snippet for the old-style
    "mount each and every sp-* device within the container" behavior.
    """
    rdebug('found a Cinder container at "{name}"'.format(name=lxc_name))
    try:
        rdebug(
            'about to record the name of the Cinder LXD - "{name}" - '
            "into {confname}".format(name=lxc_name, confname=confname)
        )
        dirname = confname.parent
        rdebug(
            "- checking for the {dirname} directory".format(dirname=dirname)
        )
        if not dirname.is_dir():
            rdebug("  - nah, creating it")
            dirname.mkdir(mode=0o755)

        rdebug("- is the file there?")
        okay = False
        expected_contents = [
            "[{node}]".format(node=platform.node()),
            "SP_EXTRA_FS=lxd:{name}".format(name=lxc_name),
        ]
        if confname.is_file():
            rdebug("  - yes, it is... but does it contain the right data?")
            contents = confname.read_text(encoding="ISO-8859-15").splitlines()
            if contents == expected_contents:
                rdebug("   - whee, it already does!")
                okay = True
            else:
                rdebug("   - it does NOT: {lst}".format(lst=contents))
        else:
            rdebug("   - nah...")
            if confname.exists():
                rdebug("     - but it still exists?!")
                subprocess.call(["rm", "-rf", "--", str(confname)])
                if confname.exists():
                    rdebug(
                        "     - could not remove it, so leaving it "
                        "alone, I guess"
                    )
                    okay = True

        if not okay:
            rdebug(
                "- about to recreate the {confname} file".format(
                    confname=confname
                )
            )
            with tempfile.NamedTemporaryFile(dir="/tmp", mode="w+t") as spconf:
                print("\n".join(expected_contents), file=spconf)
                spconf.flush()
                subprocess.check_call(
                    [
                        "install",
                        "-o",
                        "root",
                        "-g",
                        "root",
                        "-m",
                        "644",
                        "--",
                        spconf.name,
                        str(confname),
                    ],
                    shell=False,
                )
            rdebug("- looks like we are done with it")
            rdebug(
                "- let us try to restart the storpool_block service "
                "(it may not even have been started yet, so "
                "ignore errors)"
            )
            try:
                if host.service_running("storpool_block"):
                    rdebug(
                        "  - well, it does seem to be running, "
                        "so restarting it"
                    )
                    host.service_restart("storpool_block")
                else:
                    rdebug("  - nah, it was not running at all indeed")
            except Exception as e:
                rdebug(
                    "  - could not restart the service, but "
                    "ignoring the error: {e}".format(e=e)
                )
    except Exception as e:
        rdebug(
            "could not check for and/or recreate the {confname} "
            'storpool_block config file adapted the "{name}" '
            "LXD container: {e}".format(confname=confname, name=lxc_name, e=e)
        )


@reactive.when("storpool-block-charm.lxd")
def reconfigure_cinder_lxd():
    """
    Instruct storpool_block to create devices in a container's filesystem.
    """
    rdebug("reconfigure_cinder_lxd() invoked")
    reactive.remove_state("storpool-block-charm.lxd")
    cinder_name = unitdata.kv().get(kvdata.KEY_LXD_NAME)
    block_confname = BLOCK_CONFFILE
    if cinder_name is None or cinder_name == "":
        rdebug("no Cinder containers to tell storpool_block about")
        remove_block_conffile(block_confname)
        return
    rdebug("- analyzing a machine name: {name}".format(name=cinder_name))

    migrate = BlockMirrorMigrate(cinder_name)
    migrate.detect()
    rdebug("whee: migrate {migrate}".format(migrate=migrate))
    if migrate.done():
        rdebug("The mirror-dir migration seems to be complete")
        return

    if migrate.container_config is None:
        rdebug("No Cinder container running, no mirror-dir handling")
        return

    if migrate.storpool_mirror_dir is None:
        rdebug("No mirror dir defined for storpool_block, using the old way")
        create_block_conffile(migrate.container_config["name"], block_confname)
        return

    rdebug("Attempting the mirror-dir migration")
    res = migrate.run()
    rdebug("migrate.run() returned {res}".format(res=res))
    if res:
        remove_block_conffile(block_confname)


def ready():
    """
    When the StorPool block service has been installed and the OpenStack
    integration has been installed everywhere, set the unit's status to
    `active`.
    """
    rdebug("ready to go")
    try_announce()
    update_status()


def run(reraise=False):
    def reraise_or_fail():
        if reraise:
            raise
        else:
            nonlocal failed
            failed = True

    failed = False
    try:
        reactive.remove_state("storpool-block-charm.services-started")
        rdebug("Run, block, run!")
        run_block.run()
        reactive.set_state("storpool-block-charm.services-started")
        rdebug("It seems that the storpool-block setup has run its course")
        ready()
    except sperror.StorPoolNoConfigException as e_cfg:
        hookenv.log(
            "StorPool: missing configuration: {m}".format(
                m=", ".join(e_cfg.missing)
            ),
            hookenv.INFO,
        )
    except sperror.StorPoolMissingComponentsException as e_comp:
        hookenv.log("StorPool: {e}".format(e=e_comp), hookenv.ERROR)
        reraise_or_fail()
    except sperror.StorPoolException as e:
        hookenv.log("StorPool installation problem: {e}".format(e=e))
        reraise_or_fail()

    if failed:
        exit(42)


def get_status():
    inst = reactive.is_state("storpool-block-charm.services-started")
    status = {
        "node": sputils.get_machine_id(),
        "charm-config": dict(hookenv.config()),
        "storpool-conf": read_storpool_conf(),
        "installed": inst,
        "presence": service_hook.fetch_presence(RELATIONS),
        "lxd": unitdata.kv().get(kvdata.KEY_LXD_NAME),
        "ready": False,
    }

    for name in (
        "storpool_repo_url",
        "storpool_version",
        "storpool_openstack_version",
    ):
        value = status["charm-config"].get(name)
        if value is None or value == "":
            status["message"] = "No {name} in the config".format(name=name)
            return status
    if not inst:
        status["message"] = "Packages not installed yet"
        return status

    spstatus.set("maintenance", "checking the StorPool configuration")
    rdebug("about to try to obtain our StorPool ID")
    try:
        out = subprocess.check_output(["storpool_showconf", "-ne", "SP_OURID"])
        out = out.decode()
        out = out.split("\n")
        our_id = out[0]
    except Exception as e:
        status["message"] = "Could not obtain the StorPool ID: {e}".format(e=e)
        return status

    spstatus.set("maintenance", "checking the Cinder and Nova processes...")
    found = False
    status["proc"] = {}
    for cmd in ("cinder-volume", "nova-compute"):
        d = osi.check_spopenstack_processes(cmd)
        if d:
            found = True
        status["proc"][cmd] = d
        bad = sorted(filter(lambda pid: not d[pid], d.keys()))
        if bad:
            status["message"] = "No spopenstack group: {pid}".format(pid=bad)
            return status

    if found:
        spstatus.set("maintenance", "checking for the spool directory")
        dirname = pathlib.Path("/var/spool/openstack-storpool")
        if not dirname.is_dir():
            status["message"] = "No {d} directory".format(d=dirname)
            return status
        st = dirname.stat()
        if not st.st_mode & 0o0020:
            status["message"] = "{d} not group-writable".format(d=dirname)
            return status

    spstatus.set("maintenance", "checking the StorPool services...")
    svcs = ("storpool_beacon", "storpool_block")
    rdebug("checking for services: {svcs}".format(svcs=svcs))
    missing = list(filter(lambda s: not host.service_running(s), svcs))
    rdebug("missing: {missing}".format(missing=missing))
    if missing:
        status["message"] = "StorPool services not running: {missing}".format(
            missing=" ".join(missing)
        )
        return status

    spstatus.set("maintenance", "querying the StorPool API")
    rdebug("checking the network status of the StorPool client")
    try:
        out = subprocess.check_output(["storpool", "-jB", "service", "list"])
        out = out.decode()
        data = json.loads(out)
        rdebug("got API response: {d}".format(d=data))
        if "error" in data:
            raise Exception(
                "API response: {d}".format(d=data["error"]["descr"])
            )
        state = data["data"]["clients"][our_id]["status"]
        rdebug("got our client status {st}".format(st=state))
        if state != "running":
            status["message"] = "StorPool client: {st}".format(st=state)
            return status
    except Exception as e:
        status["message"] = "Could not query the StorPool API: {e}".format(e=e)
        return status

    spstatus.set("maintenance", "querying the StorPool API for client status")
    rdebug("checking the status of the StorPool client")
    try:
        out = subprocess.check_output(["storpool", "-jB", "client", "status"])
        out = out.decode()
        data = json.loads(out)
        rdebug("got API response: {d}".format(d=data))
        if "error" in data:
            raise Exception(
                "API response: {d}".format(d=data["error"]["descr"])
            )
        int_id = int(our_id)
        found = list(filter(lambda e: e["id"] == int_id, data["data"]))
        if not found:
            raise Exception(
                "No client status reported for {our_id}".format(our_id=our_id)
            )
        state = found[0]["configStatus"]
        status["message"] = "StorPool client: {st}".format(st=state)

        if state == "ok":
            status["ready"] = True
            rdebug("get_status: calling for Cinder LXD reconfiguration")
            reactive.set_state("storpool-block-charm.lxd")
        else:
            status["ready"] = False
        return status
    except Exception as e:
        status["message"] = "Could not query the StorPool API: {e}".format(e=e)
        return status


@reactive.when("storpool-block-charm.sp-run")
def sp_run():
    # Yes, removing it at once, not after the fact.  If something
    # goes wrong, the action may be reissued.
    reactive.remove_state("storpool-block-charm.sp-run")
    try:
        run(reraise=True)
    except BaseException as e:
        s = "Could not rerun the StorPool configuration: {e}".format(e=e)
        hookenv.log(s, hookenv.ERROR)
        hookenv.action_fail(s)


@reactive.hook("update-status")
def update_status():
    try:
        status = get_status()
        spstatus.set(
            "active" if status["ready"] else "maintenance", status["message"]
        )
    except BaseException as e:
        s = "Querying the StorPool status: {e}".format(e=e)
        hookenv.log(s, hookenv.ERROR)
        spstatus.set("maintenance", s)


@reactive.when("storpool-block-charm.sp-status")
def sp_status():
    # Yes, removing it at once, not after the fact.  If something
    # goes wrong, the action may be reissued.
    reactive.remove_state("storpool-block-charm.sp-status")
    try:
        status = get_status()
        hookenv.action_set({"status": json.dumps(status)})
        spstatus.set(
            "active" if status["ready"] else "maintenance", status["message"]
        )
    except BaseException as e:
        s = "Querying the StorPool status: {e}".format(e=e)
        hookenv.log(s, hookenv.ERROR)
        hookenv.action_fail(s)


@reactive.hook("stop")
def stop_and_propagate():
    """
    Propagate a `stop` action to the lower layers; in particular, let
    the `storpool-openstack-integration` layer know that it does not need to
    propagate the `stop` action by itself.

    Also set the "storpool-block-charm.stopped" state so that no further
    presence or status updates are sent to other units or charms.
    """
    rdebug("a stop event was received")

    rdebug("letting storpool-block know")
    run_block.stop()

    rdebug("done here, it seems")
    reactive.set_state("storpool-block-charm.stopped")
