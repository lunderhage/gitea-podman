# Separate Compose test: stop/start a container from another container

Run this experiment on primary2 as the same regular user that runs rootless
Podman. Copy the new test files into the local checkout at
`~/git-local/gitea-podman`. The shared SSHFS checkout cannot be bind-mounted by
Podman on this VM. Enable the user Podman socket if it is not already enabled:

```sh
systemctl --user enable --now podman.socket
cd ~/git-local/gitea-podman
./scripts/test-container-lifecycle.sh
```

The script uses a separate Compose file at tests/lifecycle/compose.yaml and the
project name gitea-lifecycle-test. It runs a disposable Alpine target with
`restart: always` and a continuously running controller on a dedicated shared
network. The controller has access to the regular user's rootless Podman socket,
so it can run Podman stop/start against the test target. There are no Gitea
volumes, cloud credentials, or published ports in this Compose file.

The existing project's management image supplies Python and the Podman client;
it is built from the existing Containerfile only if missing. An installed
podman-compose provider is used when present. Otherwise the provider runs in a
short-lived management container. No dependencies are installed on the host.
The script verifies the engine is rootless and refuses collisions with unrelated
containers or networks using the reserved test names. It does not change any
AppArmor profiles.

## What the test checks

1. Both services run on the same network; the target's restart policy is always.
2. From inside the controller, stop the target with a 30-second graceful timeout.
3. Require a successful stop exit status, target stopped, and target exit code 0.
4. Observe the target for 15 seconds. It must stay stopped while the controller
   remains running with the same start timestamp.
5. Start the target from inside the controller and confirm it is running again.
6. Repeat the cycle three times.

The test takes at least 45 seconds plus startup and command overhead. A cleanup
error from Podman is a failure even when the target process actually exited.
Any failure attempts to restart only the recorded test target and preserves the
containers for inspection. An interrupted run is reported as a failure and uses
the same recovery path. The operation lock is kept only in the runner shell, not
in inherited Podman/conmon processes.

## Read the result

Output, container inspections, network membership, pod membership, versions,
and readable AppArmor profile files are saved under:

```text
test-results/lifecycle/<timestamp>-run.<suffix>/
```

The report.txt file says PASS or FAIL, records the start/end times, and prints
an exact kernel-journal command for that interval. Run that printed command with
sudo if needed to inspect AppArmor denials. Existing overrides are recorded in
apparmor-profiles.txt where readable; the test does not undo them. A PASS with an
active override demonstrates the lifecycle under that policy and does not prove
that unmodified Ubuntu policy works.

The runner leaves both test containers available after success or failure. After
collecting diagnostics, remove only the separate experiment with:

```sh
./scripts/test-container-lifecycle.sh cleanup
```

Cleanup has its own log/report. It may itself reproduce the network-helper
AppArmor error when the last test container is stopped. A cleanup failure is
reported and remaining resources are preserved for inspection; do not use
`podman system reset`, stop all containers, or remove production Gitea volumes.

You can check the target's shell shutdown behavior locally without Podman:

```sh
./tests/lifecycle/test-target.sh
```

That checks the actual Compose command using the installed /bin/sh, not the
Alpine image or the full container lifecycle. The implementation workspace cannot
initialize its Podman client, so the live three-cycle result must be obtained on
primary2. This experiment does not yet validate Gitea dump, encrypted backup, or
restore.
