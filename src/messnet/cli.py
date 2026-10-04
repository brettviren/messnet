"""messnet command line interface (Click commands only; logic lives in library modules)."""

import asyncio
import json
import logging
import signal
import sys

import click

from messnet import daemon, manager, netdetect, peers, state
from messnet.config import load_config
from messnet.event import EventError, default_source, dumps, parse_assignments
from messnet.filters import parse_since
from messnet.links import check_spec, keep_link, link_label, run_link, serve_tcp
from messnet.node import Node
from messnet.replicate import LINK_EVENT, LINK_SCOPES

CONTEXT = {"help_option_names": ["-h", "--help"]}


def _node(ctx: click.Context) -> Node:
    node = Node(ctx.obj)
    ctx.call_on_close(node.close)
    return node


def _run_async(coro) -> None:
    """Run CORO; SIGTERM / SIGINT cancel it so sessions shut down cleanly."""
    async def main():
        task = asyncio.ensure_future(coro)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


def _format_text(ev: dict) -> str:
    data = json.dumps(ev.get("data"), ensure_ascii=False) if "data" in ev else ""
    return f"{ev['time']} {ev['origin']} {ev['type']} {ev.get('subject') or '-'} {data}".rstrip()


def _echo_events(events, fmt: str) -> None:
    for ev in events:
        click.echo(dumps(ev) if fmt == "json" else _format_text(ev))


@click.group(context_settings=CONTEXT, invoke_without_command=True)
@click.option("-c", "--config", "config_path", type=click.Path(dir_okay=False),
              help="Config file (default $MESSNET_CONFIG or ~/.config/messnet/config.toml).")
@click.option("--node", help="Node name (default HOST.USER).")
@click.option("--spool", type=click.Path(file_okay=False), help="Shared ('all' scope) spool directory.")
@click.option("--db", type=click.Path(dir_okay=False), help="Local SQLite store.")
@click.option("--state", type=click.Path(file_okay=False), help="Materialized state directory.")
@click.option("--etc", type=click.Path(file_okay=False), help="Shared peers/networks directory.")
@click.option("-n", "--network", help="Force a network profile instead of detecting one.")
@click.option("-v", "--verbose", count=True, help="More logging to stderr (repeatable).")
@click.pass_context
def cli(ctx, config_path, node, spool, db, state, etc, network, verbose):
    """Multi-host event notification network."""
    logging.basicConfig(stream=sys.stderr, format="messnet: %(levelname)s %(message)s",
                        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(verbose, 2)])
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        ctx.exit()
    try:
        ctx.obj = load_config(config_path, node=node, spool=spool, db=db, state=state,
                              etc=etc, network=network)
    except (OSError, ValueError) as err:
        raise click.ClickException(str(err)) from err


@cli.command(context_settings=CONTEXT, no_args_is_help=True)
@click.argument("type", required=False)
@click.argument("assignments", nargs=-1)
@click.option("-s", "--subject", help="Event subject (what within the source it is about).")
@click.option("--scope", type=click.Choice(["host", "lan", "all"]), default="all", show_default=True,
              help="How far the event may travel.")
@click.option("-P", "--producer", default="cli", show_default=True, help="Producer name used in the default source.")
@click.option("--source", help="Explicit CloudEvents source URI.")
@click.option("-d", "--data", "data_json", help="Event data as a JSON value ('-' reads stdin).")
@click.option("--stdin", "from_stdin", is_flag=True,
              help="Read JSON lines, each a partial event with at least \"type\".")
@click.option("-p", "--print", "echo", is_flag=True, help="Print the emitted event(s).")
@click.pass_context
def emit(ctx, type, assignments, subject, scope, producer, source, data_json, from_stdin, echo):
    """Emit an event of TYPE with data from key=value / key:=json ASSIGNMENTS.

    \b
    messnet emit backup.run.v1 -s push phase=push result=ok
    messnet emit host.load.v1 load:=0.42
    producer | messnet emit --stdin
    """
    node = _node(ctx)
    try:
        if from_stdin:
            events = [node.emit_object(json.loads(line)) for line in sys.stdin if line.strip()]
        else:
            if not type:
                raise click.UsageError("TYPE is required unless --stdin is given")
            data = parse_assignments(list(assignments)) or None
            if data_json is not None:
                data = json.loads(sys.stdin.read() if data_json == "-" else data_json)
            events = [node.emit(type, data, subject=subject, scope=scope,
                                source=source or default_source(producer))]
    except (EventError, json.JSONDecodeError) as err:
        raise click.ClickException(str(err)) from err
    if echo:
        _echo_events(events, "json")


@cli.command(context_settings=CONTEXT, no_args_is_help=True)
@click.option("-t", "--type", "types", multiple=True, help="Type glob, e.g. 'backup.*' (repeatable).")
@click.option("-o", "--origin", help="Only events from this origin node.")
@click.option("--since", help="Only events newer than an age (30m, 2h, 1d) or ISO time.")
@click.option("-f", "--follow", is_flag=True, help="Keep printing new events as they arrive.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="json", show_default=True)
@click.pass_context
def tail(ctx, types, origin, since, follow, fmt):
    """Print stored events (use -t '*' for all)."""
    node = _node(ctx)
    try:
        since_ts = parse_since(since)
    except ValueError as err:
        raise click.BadParameter(str(err), param_hint="--since") from err
    try:
        _echo_events((ev for _, ev in node.follow(list(types), origin, since_ts, forever=follow)), fmt)
    except KeyboardInterrupt:
        pass


@cli.command(context_settings=CONTEXT)
@click.pass_context
def ingest(ctx):
    """Scan the spools for new events and store them."""
    click.echo(f"{_node(ctx).ingest()} new events")


@cli.command(context_settings=CONTEXT)
@click.pass_context
def vv(ctx):
    """Print this node's version vector (stream -> contiguous seq)."""
    for key, seq in sorted(_node(ctx).store.version_vector().items()):
        click.echo(f"{key} {seq}")


@cli.command("config", context_settings=CONTEXT)
@click.pass_context
def config_cmd(ctx):
    """Print the effective configuration as JSON."""
    click.echo(json.dumps(ctx.obj.as_dict(), indent=2))


@cli.group("state", context_settings=CONTEXT, no_args_is_help=True)
def state_grp():
    """Materialized latest-event state for fast consumers."""


@state_grp.command("get", context_settings=CONTEXT)
@click.argument("types", nargs=-1)
@click.option("-o", "--origin", help="Only state from this origin node.")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="json", show_default=True)
@click.pass_context
def state_get(ctx, types, origin, fmt):
    """Print latest events matching type globs TYPES (default all)."""
    _echo_events(state.read(ctx.obj.state, list(types), origin), fmt)


@state_grp.command("update", context_settings=CONTEXT)
@click.option("--rebuild", is_flag=True, help="Reconsider all stored events, not only new ones.")
@click.pass_context
def state_update(ctx, rebuild):
    """Ingest the spool and write state files for new events."""
    node = _node(ctx)
    node.ingest()
    click.echo(f"{state.update(node.store, ctx.obj.state, rebuild)} state files written")


_scope_opt = click.option("--scope", type=click.Choice(LINK_SCOPES), default="all", show_default=True,
                          help="Link scope: 'lan' also carries lan-scoped events.")


@cli.command(context_settings=CONTEXT, no_args_is_help=True)
@click.option("--stdio", is_flag=True, help="Speak the protocol on stdin/stdout (for ssh).")
@click.option("--maintain/--no-maintain", default=True, show_default=True,
              help="Also ingest the spool and update state while linked.")
@_scope_opt
@click.pass_context
def link(ctx, stdio, maintain, scope):
    """Serve one replication session (remote end of ssh / cmd links)."""
    if not stdio:
        raise click.UsageError("only --stdio is supported")
    _run_async(daemon.run_stdio(_node(ctx), scope, maintain))


@cli.command(context_settings=CONTEXT, no_args_is_help=True)
@click.option("-l", "--listen", "addrs", multiple=True, required=True, help="HOST:PORT to accept links on.")
@_scope_opt
@click.pass_context
def serve(ctx, addrs, scope):
    """Accept TCP links (and keep spool/state maintained)."""
    node = _node(ctx)

    async def main():
        servers = [await serve_tcp(node, a, scope) for a in addrs]
        try:
            await daemon.maintain(node)
        finally:
            for s in servers:
                s.close()

    _run_async(main())


@cli.group(context_settings=CONTEXT, no_args_is_help=True)
def connect():
    """Open a link to a peer and replicate until it closes."""


def _connect(ctx, spec: dict, keep: bool) -> None:
    try:
        check_spec(spec)
    except ValueError as err:
        raise click.UsageError(str(err)) from err
    node = _node(ctx)
    _run_async(keep_link(node, spec) if keep else run_link(node, spec))


_keep_opt = click.option("-k", "--keep", is_flag=True, help="Reconnect forever with backoff.")


@connect.command("tcp", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("addr")
@_scope_opt
@_keep_opt
@click.pass_context
def connect_tcp(ctx, addr, scope, keep):
    """Link to a 'messnet serve' at HOST:PORT ADDR."""
    _connect(ctx, {"kind": "tcp", "addr": addr, "scope": scope}, keep)


@connect.command("ssh", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("target")
@click.option("--command", help="Remote command (default 'messnet link --stdio').")
@_scope_opt
@_keep_opt
@click.pass_context
def connect_ssh(ctx, target, command, scope, keep):
    """Link over ssh to TARGET (an ssh alias or user@host)."""
    _connect(ctx, {"kind": "ssh", "target": target, "command": command, "scope": scope}, keep)


@connect.command("cmd", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("argv", nargs=-1, required=True)
@_scope_opt
@_keep_opt
@click.pass_context
def connect_cmd(ctx, argv, scope, keep):
    """Link over the stdin/stdout of a command, e.g. gonc (use -- before ARGV)."""
    _connect(ctx, {"kind": "cmd", "argv": list(argv), "scope": scope}, keep)


@connect.command("iroh", context_settings=CONTEXT, no_args_is_help=True)
@click.argument("id")
@click.option("-a", "--addr", "addrs", multiple=True, help="Direct HOST:PORT address of the peer (repeatable).")
@click.option("--relay", help="Relay URL the peer uses.")
@_scope_opt
@_keep_opt
@click.pass_context
def connect_iroh(ctx, id, addrs, relay, scope, keep):
    """Link over iroh to the node with endpoint ID."""
    spec = {"kind": "iroh", "id": id, "addrs": list(addrs), "scope": scope}
    if relay:
        spec["relay"] = relay
    _connect(ctx, spec, keep)


@cli.command("id", context_settings=CONTEXT)
@click.pass_context
def id_cmd(ctx):
    """Print this node's id (public key; also its iroh endpoint id)."""
    from messnet import keys
    click.echo(keys.public_id(keys.load_seed(ctx.obj.key)))


@cli.command(context_settings=CONTEXT)
@click.pass_context
def run(ctx):
    """Run the node daemon: maintenance plus configured listeners and links."""
    try:
        _run_async(daemon.run(_node(ctx)))
    except ValueError as err:
        raise click.ClickException(str(err)) from err


@cli.command(context_settings=CONTEXT)
@click.option("--facts", "show_facts", is_flag=True, help="Also print the gathered network facts.")
@click.pass_context
def net(ctx, show_facts):
    """Show the detected network profile."""
    facts = netdetect.gather_facts()
    profile = netdetect.select_profile(peers.load_networks(ctx.obj.etc), facts, ctx.obj.network)
    out = {"profile": profile, "facts": facts} if show_facts else profile
    click.echo(json.dumps(out, indent=2))


@cli.group("peers", context_settings=CONTEXT, no_args_is_help=True)
def peers_grp():
    """The shared peer directory (etc/peers/*.toml)."""


@peers_grp.command("list", context_settings=CONTEXT)
@click.pass_context
def peers_list(ctx):
    """List known peers and the link kinds they advertise."""
    for name, rec in peers.load_peers(ctx.obj.etc).items():
        kinds = ",".join(k for k, v in rec.items() if isinstance(v, list) and v and isinstance(v[0], dict))
        click.echo(f"{name} {rec.get('updated', '-')} {kinds or '-'}")


@peers_grp.command("publish", context_settings=CONTEXT)
@click.pass_context
def peers_publish(ctx):
    """Write this node's peer file from the 'advertise' configuration."""
    node = _node(ctx)
    if not ctx.obj.advertise:
        raise click.ClickException("nothing to publish: no 'advertise' in configuration")
    click.echo("published" if daemon.publish_self(node) else "unchanged")


@peers_grp.command("plan", context_settings=CONTEXT)
@click.pass_context
def peers_plan(ctx):
    """Show which links the manager would try, per peer, on this network."""
    profile, plan = manager.current_plan(_node(ctx), netdetect.gather_facts())
    click.echo(f"network: {profile['name']} links={','.join(profile['links'])} scope={profile['scope']}")
    for name, specs in plan.items():
        click.echo(f"{name}:" + ("" if specs else " (no usable links)"))
        for spec in specs:
            click.echo(f"  {spec['scope']:3} {link_label(spec)}")


@cli.command(context_settings=CONTEXT)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text", show_default=True)
@click.pass_context
def links(ctx, fmt):
    """Show the last reported state of each peer link (from 'run'/'serve')."""
    node = _node(ctx)
    state.update(node.store, ctx.obj.state)
    for ev in state.read(ctx.obj.state, [LINK_EVENT], ctx.obj.node):
        if fmt == "json":
            click.echo(dumps(ev))
            continue
        d = ev.get("data", {})
        extra = f" error={d['error']}" if d.get("error") else ""
        click.echo(f"{ev['subject']:20} {d.get('state', '?'):4} {ev['time']} {d.get('link', '')}{extra}")
