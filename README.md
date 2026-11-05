# utmapp-bot

A Claude Code plugin for building, testing and debugging
[UTM](https://github.com/utmapp/UTM) on Apple silicon. Its skills set up
test and development VMs, build UTM and its dependencies, drive and measure
VMs, debug the VirtIO GPU stack (Neptune D3D and Venus Vulkan), and run
Auto-Triage for the UTM games database.

## Prerequisites

- An Apple silicon Mac and [Claude Code](https://claude.com/claude-code),
  logged in.
- A current UTM (`/Applications/UTM.app`). Older releases lack scripting
  the plugin uses; the plugin stops and says so.
- Python 3 (standard library only).
- macOS permissions for the terminal: Automation for UTM, and Screen
  Recording for frame sampling. macOS asks the first time.
- Per task:
  - a Windows 11 ARM64 ISO to provision Windows VMs;
  - Xcode and a UTM checkout to build UTM (`build-utm`);
  - `gh`, logged in, for skills that read issues or publish fix branches
    (as the bot account, with the `repo` or `notifications` scope, for
    `github-setup`);
  - CrossOver with a signed-in Steam bottle for `crossover-compare`;
  - access to the games database server for the `db-*` skills.

[docs/utm-host.md](docs/utm-host.md) covers what the skills assume about the
Mac: which UTM runs, signing and networking, and the macOS prompts.

## Install

```sh
claude plugin marketplace add <path or GitHub repo of this repository>
claude plugin install utmapp-bot@utmapp-bot
```

Inside a checkout of this repository, `.claude/settings.json` loads the
plugin in place.

## Using the skills

Ask Claude for what you want ("set up a Windows test VM", "triage this
issue", "is this a D3DMetal bug?") and it picks the skill, or call one
directly as `/utmapp-bot:<skill> <arguments>`. A typical first session:

```
/utmapp-bot:provision-windows-dev-vm ~/Downloads/Win11_ARM64.iso
/utmapp-bot:provision-windows-test-vm ~/Downloads/Win11_ARM64.iso
/utmapp-bot:run-gpu-test-vm boot it and check D3D works
```

The skills share a harness, `bin/utmbot` (`bin/utmbot help` lists its
commands; `bin/utmbot app` shows which UTM it drives). State, caches and
logs live in `~/.utmapp-bot/` (or `$UTMAPP_BOT_DATA`), outside the plugin.

## Skills

### VMs

| Skill | Does |
|---|---|
| `provision-windows-dev-vm <iso>` | Windows 11 ARM64 build and kernel-debugger VM: Visual Studio, WDK, SSH, a share of the Mac |
| `provision-windows-test-vm <iso>` | Windows 11 ARM64 VM to test drivers on: test signing, kdnet debugging, benchmarks |
| `provision-linux-test-vm` | Ubuntu arm64 VM with Venus, and D3D under Wine and FEX |
| `provision-macos-vm <version>` | macOS VM (12 to the newest) from an IPSW or the latest one, with an admin account, SSH and no sleep or lock; Setup Assistant driven through the screen |
| `install-xcode-vm` | An older Xcode and iOS simulator runtimes in a macOS VM, for iOS versions the host's Xcode dropped |
| `run-gpu-test-vm` | Boot a Windows or Linux GPU test VM disposable, install a driver, run programs, capture the screen, read host logs |

### Build

| Skill | Does |
|---|---|
| `build-utm <checkout> [project]` | Rebuild a UTM dependency (QEMU, virglrenderer, …) or UTM itself into a dev UTM.app, and prove the change is in it |
| `build-viogpu-driver` | Build and test-sign the VirtIO GPU Windows driver on a dev VM |

### Test and debug

| Skill | Does |
|---|---|
| `repro-gpu-issue <issue>` | Reproduce a 3D graphics problem (a UTM issue or a games-db report) at the reporter's settings and draft a reply |
| `test-utm-ui` | Run a UTM build on several macOS versions (VMs) and iOS versions (simulators), screenshot it, debug it in a VM |
| `triage-gpu-issue <symptom>` | Playbooks for hangs, black screens, corruption, GPU faults, crashes |
| `attribute-to-layer <bug>` | Decide which layer owns a bug: ours, the backend, Wine or UTM's display |
| `kernel-debug` | kd over kdnet: live driver logs, break-in, bugcheck analysis |
| `crossover-compare <game>` | Run the same game in CrossOver as an A/B partner |
| `perf-regression` | Valid benchmark runs and A/B comparisons |
| `validate-stack` | The full backend × workload matrix before calling a change safe |
| `write-gpu-probe <symptom>` | A minimal D3D11/D3D12/Vulkan repro program |

### Games database

The UTM games database collects compatibility reports. These skills triage
and moderate them; `db-setup` lets a Mac run them unattended.

| Skill | Does |
|---|---|
| `db-setup` | Once per Mac: the tunnel to the server, backups, and a dispatcher that runs the skills below unattended |
| `db-triage <report>` | Auto-Triage one report end to end |
| `db-summarize <report>` | Public technical notes from a report's private attachments |
| `db-moderate` | Flags, spam and deletion requests |
| `db-steam-load`, `db-steam-install`, `db-steam-accept`, `db-steam-cleanup` | The Steam VM: baseline, game snapshots, gifts, disk space |

[docs/gamesdb.md](docs/gamesdb.md) covers how it works.

### UTM pull requests and issues

| Skill | Does |
|---|---|
| `review-utm-pr <PR>` | Review and test a utmapp/UTM pull request: UTM's own review (blockers only), screenshots on every configuration CONTRIBUTING.md recommends, one comment with the results |
| `answer-utm-issue <issue>` | Answer a UTM issue of any kind: reproduce it where it happens, find the cause, and reply; a small fix is opened as a PR, a large one gets a fix outline |
| `github-setup` | Once per Mac: pick up `@`-mentions (PRs and issues), review requests and follow-up comments for the bot account in unattended sessions (several Macs share the work) |

### Process

| Skill | Does |
|---|---|
| `land-stack-change` | Turn an investigation into clean commits across repos |
| `investigation-handoff` | Write or resume a handoff between sessions |
| `test-plugin` | End-to-end tests of the plugin against a local games database |

## Contributing

Run `scripts/check-portable.sh` before committing; it keeps machine-specific
paths, addresses and identities out of the repository.

## License

[Apache License 2.0](LICENSE).
