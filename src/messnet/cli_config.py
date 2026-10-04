"""'messnet config' commands: view and edit config.toml (logic in messnet.confedit)."""

import json

import click

from messnet import confedit, daemon, peers
from messnet.config import load_config

CONTEXT = {"help_option_names": ["-h", "--help"]}


def _file(ctx):
    return ctx.find_root().meta["config_file"]


def _edit(ctx, change):
    """Load the config file, apply CHANGE(doc), save; return CHANGE's result."""
    path = _file(ctx)
    doc = confedit.load(path)
    try:
        result = change(doc)
    except (ValueError, FileNotFoundError) as err:
        raise click.ClickException(str(err)) from err
    confedit.save(doc, path)
    return result


def _republish(ctx) -> None:
    cfg = load_config(_file(ctx))
    changed = daemon.publish_config(cfg)
    click.echo(f"peer file {'updated' if changed else 'unchanged'}: {cfg.etc}/peers/{cfg.node}.toml")


def _host(ctx, host):
    return host or ctx.obj.node


@click.group("config", context_settings=CONTEXT, no_args_is_help=True)
def config_grp():
    """View and edit the configuration file.

    The file is -c FILE, $MESSNET_CONFIG or ~/.config/messnet/config.toml;
    it is created on first edit.

    \b
    Directories (set once per node):
      messnet config set node alpha
      messnet config set etc ~/sync/messnet/etc       shared peer directory
      messnet config set spool ~/sync/messnet/spool   shared event spool
    Share both directories with Syncthing between your hosts.  etc holds
    each node's peer file (its id and how to reach it); spool lets events
    flow as files even with no links.

    \b
    Connectivity methods (KIND):
      tcp   direct TCP to HOST:PORT (LAN, Tailscale, ssh -L tunnel)
      ssh   ssh to a host that runs 'messnet link --stdio' on demand
      cmd   any command whose stdin/stdout reach 'messnet link --stdio'
      iroh  QUIC by node id; no public relays (needs the iroh extra)
      gonc  gonc TLS-PSK TCP or NAT-traversing P2P (needs gonc)

    \b
    For this node, 'add' records how OTHERS reach it: an [advertise]
    entry published to etc/peers/<node>.toml plus what serves it (a tcp
    listener, iroh bind address, gonc responder) used by 'messnet run':
      messnet config add tcp 100.96.203.121:7701
      messnet config add ssh alpha
      messnet config add iroh --addr 100.96.203.121:7702 --bind 0.0.0.0:7702
      messnet config add gonc --mode tcp --addr 203.0.113.5:7703 --new-secret alpha-gonc
      messnet config add cmd -- ssh -J jump alpha messnet link --stdio

    \b
    With --host NODE, 'add' records how THIS node reaches NODE (a [[links]]
    entry used by 'messnet run'), e.g. when NODE publishes nothing usable:
      messnet config add --host beta ssh beta.example.org
      messnet config add --host beta tcp 10.0.0.7:7701

    \b
    Show and remove methods (numbers come from 'methods'):
      messnet config methods [--host NODE]
      messnet config remove [--host NODE] tcp 1
      messnet config remove [--host NODE] ssh --all

    See 'messnet config add KIND -h' for each kind's options.
    """


@config_grp.command("show", context_settings=CONTEXT)
@click.option("--file", "raw", is_flag=True, help="Print the config file itself instead.")
@click.pass_context
def show(ctx, raw):
    """Print the effective configuration (JSON) or the raw file."""
    path = _file(ctx)
    if raw:
        click.echo(f"# {path}")
        click.echo(path.read_text() if path.exists() else "# (does not exist yet)")
        return
    click.echo(json.dumps(ctx.obj.as_dict(), indent=2))


@config_grp.command("set", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("key", type=click.Choice(list(confedit.SCALARS)))
@click.argument("value")
@click.pass_context
def set_cmd(ctx, key, value):
    """Set KEY to VALUE (directories are created; peers: "*" or a,b,c)."""
    val = _edit(ctx, lambda doc: confedit.set_scalar(doc, key, value))
    click.echo(f"{key} = {val!r} in {_file(ctx)}")


@config_grp.command("unset", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("key", type=click.Choice(list(confedit.SCALARS)))
@click.pass_context
def unset_cmd(ctx, key):
    """Remove KEY from the config file (its default applies again)."""
    removed = _edit(ctx, lambda doc: confedit.unset_scalar(doc, key))
    click.echo(f"{key} {'removed' if removed else 'was not set'}")


@config_grp.command("methods", context_settings=CONTEXT)
@click.option("--host", help="Another node (default: this node).")
@click.pass_context
def methods(ctx, host):
    """Show connectivity methods of this node or of --host NODE."""
    cfg = confedit.load(_file(ctx)).unwrap()
    me = ctx.obj.node
    host = _host(ctx, host)
    published = peers.load_peers(ctx.obj.etc).get(host)
    for line in confedit.methods_report(cfg, me, host, published):
        click.echo(line)


@config_grp.group("add", context_settings=CONTEXT, no_args_is_help=True)
@click.option("--host", help="Add a way for this node to reach NODE instead of advertising.")
@click.pass_context
def add(ctx, host):
    """Add a connectivity method (see 'messnet config -h')."""
    ctx.meta["host"] = host


_scope = click.option("--scope", type=click.Choice(["all", "lan"]), default="all", show_default=True,
                      help="Widest event scope over this method.")
_nets = click.option("--networks", help="Only usable on these network profiles (comma list).")
_ts = click.option("--tailscale", is_flag=True, help="Only usable by dialers with tailscale up.")


def _add(ctx, kind: str, fields: dict) -> None:
    host = ctx.meta.get("host")
    me = ctx.obj.node
    if fields.get("networks"):
        fields["networks"] = [n.strip() for n in fields["networks"].split(",") if n.strip()]
    if fields.get("scope") == "all":
        fields.pop("scope")
    if host and host != me:
        spec = _edit(ctx, lambda doc: confedit.add_link(doc, host, kind, fields))
        click.echo(f"added {kind} link to {host}: {confedit.describe(kind, spec)}")
        return
    ad = _edit(ctx, lambda doc: confedit.add_own(doc, kind, fields))
    click.echo(f"advertised {kind}: {confedit.describe(kind, ad)}")
    _republish(ctx)


@add.command("tcp", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("addr")
@click.option("--listen", help="Local HOST:PORT to listen on (default: ADDR).")
@_scope
@_nets
@_ts
@click.pass_context
def add_tcp(ctx, addr, listen, scope, networks, tailscale):
    """Direct TCP at ADDR (HOST:PORT reachable by peers).

    For this node also adds a listener; TCP links are authenticated
    against the peer directory.
    """
    _add(ctx, "tcp", {"addr": addr, "listen": listen, "scope": scope,
                      "networks": networks, "tailscale": tailscale})


@add.command("ssh", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("target")
@click.option("--command", help="Remote command (default 'messnet link --stdio').")
@_scope
@_nets
@_ts
@click.pass_context
def add_ssh(ctx, target, command, scope, networks, tailscale):
    """ssh to TARGET (an ssh alias or user@host); needs non-interactive ssh.

    Nothing needs to run on the target: ssh starts 'messnet link --stdio'.
    """
    _add(ctx, "ssh", {"target": target, "command": command, "scope": scope,
                      "networks": networks, "tailscale": tailscale})


@add.command("cmd", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("argv", nargs=-1, required=True)
@_scope
@_nets
@_ts
@click.pass_context
def add_cmd(ctx, argv, scope, networks, tailscale):
    """Any command ARGV reaching 'messnet link --stdio' (put -- before ARGV)."""
    _add(ctx, "cmd", {"argv": list(argv), "scope": scope, "networks": networks,
                      "tailscale": tailscale})


@add.command("iroh", context_settings=CONTEXT)
@click.option("-a", "--addr", "addrs", multiple=True, help="Direct HOST:PORT (UDP) peers dial (repeatable).")
@click.option("--bind", help="This node: local HOST:PORT for the iroh endpoint (fix the port).")
@click.option("--relay", "relays", multiple=True, help="Self-hosted relay URL (repeatable).")
@click.option("--id", "node_id", help="With --host: the node's id (default: from its peer file).")
@_scope
@_nets
@_ts
@click.pass_context
def add_iroh(ctx, addrs, bind, relays, node_id, scope, networks, tailscale):
    """iroh (QUIC, UDP) dialed by node id; relays only if you list your own."""
    host = ctx.meta.get("host")
    fields = {"addrs": list(addrs), "scope": scope, "networks": networks, "tailscale": tailscale}
    if relays:
        fields["relay"] = relays[0]
    if host and host != ctx.obj.node:
        fields["id"] = node_id or peers.load_peers(ctx.obj.etc).get(host, {}).get("id")
        if not fields["id"]:
            raise click.UsageError(f"no id known for {host}: publish its peer file or give --id")
    else:
        fields.update(bind=bind, relays=list(relays))
    _add(ctx, "iroh", fields)


@add.command("gonc", context_settings=CONTEXT, no_args_is_help=True)
@click.option("--secret", help="Secret file name in the local secrets directory.")
@click.option("--new-secret", help="Create a new strong secret with this name and use it.")
@click.option("--mode", type=click.Choice(["tcp", "p2p"]), default="p2p", show_default=True)
@click.option("--addr", help="tcp mode: HOST:PORT peers dial.")
@click.option("--listen", help="tcp mode, this node: local HOST:PORT the responder listens on.")
@click.option("--lan", is_flag=True, help="p2p: LAN broadcast discovery only.")
@click.option("--mqttsrv", help="p2p: self-hosted rendezvous MQTT server(s).")
@click.option("--stunsrv", help="p2p: self-hosted STUN server(s).")
@_scope
@_nets
@_ts
@click.pass_context
def add_gonc(ctx, secret, new_secret, mode, addr, listen, lan, mqttsrv, stunsrv, scope,
             networks, tailscale):
    """gonc: TLS-PSK TCP (--mode tcp --addr) or P2P (--lan, or --mqttsrv
    and --stunsrv; neither means gonc's public servers).

    Both nodes need the same secret file in their secrets directory; copy
    it securely (e.g. scp), never via a synced folder.
    """
    if new_secret:
        try:
            path = confedit.new_secret(ctx.obj.secrets, new_secret)
        except ValueError as err:
            raise click.ClickException(str(err)) from err
        click.echo(f"created secret {path}; copy it to the peer's secrets directory")
        secret = new_secret
    if not secret:
        raise click.UsageError("give --secret NAME or --new-secret NAME")
    _add(ctx, "gonc", {"secret": secret, "mode": "tcp" if mode == "tcp" else None, "addr": addr,
                       "listen": listen, "lan": lan, "mqttsrv": mqttsrv, "stunsrv": stunsrv,
                       "scope": scope, "networks": networks, "tailscale": tailscale})


@config_grp.command("remove", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("kind", type=click.Choice(["tcp", "ssh", "cmd", "iroh", "gonc"]))
@click.argument("number", type=int, required=False)
@click.option("--all", "remove_all", is_flag=True, help="Remove every method of KIND.")
@click.option("--host", help="Remove this node's links to NODE instead.")
@click.pass_context
def remove(ctx, kind, number, remove_all, host):
    """Remove method NUMBER (see 'methods') of KIND, or --all of them."""
    if (number is None) == (not remove_all):
        raise click.UsageError("give a method NUMBER or --all")
    host = _host(ctx, host)
    if host != ctx.obj.node:
        gone = _edit(ctx, lambda doc: confedit.remove_link(doc, host, kind, number))
        click.echo(f"removed {len(gone)} {kind} link(s) to {host}")
        return
    gone = _edit(ctx, lambda doc: confedit.remove_own(doc, kind, number))
    click.echo(f"removed {len(gone)} advertised {kind} method(s)")
    _republish(ctx)
