# Recover without overwriting a live volume

1. Stop the monitor before changing its data location. Keep the existing volume and backup unchanged.
2. Validate a backup with a local restore into a new folder using the CLI. Check its history. A successful empty snapshot is valid if the source had no samples.
3. For a new Docker volume, run `docker volume create homelab-recovered`. This creates an empty destination without changing the original.
4. Restore to a new child directory of that volume. Replace `/absolute/path/new-backup` with your backup directory:

```sh
docker run --rm --user 0 --entrypoint sh -v homelab-recovered:/recovery homelab-monitor -c 'mkdir /recovery/owned && chown 10001:10001 /recovery/owned'
docker run --rm --read-only --mount type=bind,src=/absolute/path/new-backup,dst=/backup,readonly --mount type=volume,src=homelab-recovered,dst=/recovery homelab-monitor restore --backup /backup --data /recovery/owned/restored
```

The generated Compose image name depends on the project directory. Use the actual image name printed by `docker compose images`; `homelab-monitor` above is an example. The first command uses root only inside the new recovery volume to create its writable parent. No host mount is writable. Both new directories must not exist before these commands.

5. Before serving, inspect the restored history through a container using that same new volume. Add a Compose override that mounts `homelab-recovered` at `/recovery` and passes `serve --data /recovery/owned/restored --host 0.0.0.0`. Preserve the loopback host port. Keep the original volume available until the restored service passes health and history checks.

This manual external-volume procedure is documented but not covered by the initial CI flow. CI covers real snapshots/restores inside a running container and persistence through restart. Never use an unreviewed snapshot on a public or multi-user service.
