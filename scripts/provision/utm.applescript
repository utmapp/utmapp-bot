-- utm.applescript - UTM automation for the provisioning scripts and the
-- utmbot harness.
--
-- usage: osascript utm.applescript <command> <args...>
--
-- Drives the UTM.app at $UTM_APP when that is set (a dev build), otherwise
-- the app named UTM.  The release and dev apps share a bundle id, so only
-- one of them may run at a time (the harness checks).  Every command but
-- guest-tools gives up after 600 s (-1712).
--
-- It uses UTM's scripting as current UTM has it (install guest tools, the
-- start environment, capture screen, the file of a VM, and the debug log,
-- tpm, tso, rtc local time and sound devices configuration properties).  The
-- callers check the dictionary first and stop with an error on an older UTM.
--
--   exists <name>                       "true" or "false"
--   status <name>                       stopped, starting, started, ...
--   file <name>                         POSIX path of the VM's .utm bundle
--   create <name> <mac> <bundle> <nic2-hardware|none> <memory-MiB> <disk-MiB> [<ssh-port>]
--                                       create the VM, export it to <bundle>
--                                       (a .utm path) and delete it again, so
--                                       the host can edit its config.plist.
--                                       With <ssh-port> the first NIC is
--                                       emulated with 127.0.0.1:<ssh-port>
--                                       forwarded to the guest's port 22 (a
--                                       UTM without vmnet entitlement)
--   create-linux <name> <mac> <memory-MiB> <disk-MiB> <image> <seed-iso> [<ssh-port>]
--                                       create a Linux test VM from a cloud
--                                       image (imported as a VirtIO disk and
--                                       grown to <disk-MiB>) with the
--                                       cloud-init seed as a USB drive, a
--                                       virtio-gpu-gl display (Venus) and
--                                       TSO; prints the TSO setting
--   import <bundle> <boot-iso> <config-iso>
--                                       import <bundle> and insert the ISOs
--   start <name> [NAME=value ...]       start, passing the environment entries
--                                       to QEMU for this start (see
--                                       start-disposable)
--   start-disposable <name> [NAME=value ...]
--                                       start without saving disk changes,
--                                       passing the environment entries to
--                                       QEMU and the render server
--   stop <name>                         force stop
--   kill <name>                         kill the VM process
--   capture-screen <name> <png>         save the guest display
--   input-text <name> <text>            type ASCII text
--   input-scancodes <name> <code> ...   send PC AT scan codes (decimal)
--   input-click <name> <x> <y> [left|right|middle]
--                                       click at a guest screen position
--   guest-tools <name>                  UTM puts the guest tools in the VM's empty
--                                       removable drive (downloading them when
--                                       needed, up to 1700 s)
--   eject <name>                        replace the removable drives (the ISOs,
--                                       the guest tools) with one empty one, as
--                                       on the golden VM
--   nic <name> <index>                  "<mac><TAB><hardware><TAB><vlan guest
--                                       address>" of that network interface
--   net-modes <name>                    the mode of every network interface
--                                       (shared, host, bridged, emulated)
--   net-emulated <name> <ssh-port>      every network interface emulated, the
--                                       first with 127.0.0.1:<ssh-port>
--                                       forwarded to the guest's port 22;
--                                       prints the previous modes, for
--                                       net-restore.  The VM must be stopped
--   net-restore <name> <ssh-port> <mode> ...
--                                       undo net-emulated: the interfaces get
--                                       the given modes back and the first
--                                       loses the forward of <ssh-port>
--   debug-log <name> true|false         set the VM's Debug Log; the VM must
--                                       be stopped
--   create-mac <name> <ipsw|latest> <memory-MiB> <cpus> <disk-MiB>
--                                       create a macOS VM (Apple backend) from
--                                       an IPSW, or, with "latest", for the
--                                       latest IPSW this Mac supports (which
--                                       install-mac downloads); prints its MAC
--   install-mac <name>                  start installing macOS (erases the disk)
--   query-install <name>                "installing:<bool>, progress:<0-1>"
--   start-provision <name> <user> <password> <full name>
--                                       first start after install, set up
--                                       unattended (macOS 27 guest and host):
--                                       the admin account, automatic login and
--                                       Remote Login
--   kdnet-net <name> <subnet> <dhcp-end>
--                                       pin network interface 1 (host-only)
--                                       to <subnet> with DHCP up to <dhcp-end>;
--                                       the VM must be stopped
--
-- The VM mirrors the golden "Windows Development" VM: aarch64 virt with HVF,
-- UEFI, default core count, an NVMe disk, a virtio-net-pci NIC on the shared
-- network, a second NIC on the host-only kdnet network (virtio-net-pci on dev
-- VMs, e1000 on test VMs) and a virtio-ramfb-gl display, with the TPM (and
-- with it the Secure Boot firmware), local-time RTC and intel-hda sound.
--
-- Drive order is boot order: the NVMe disk comes first, so the empty disk
-- falls through to the install ISO and the installed system boots from disk.

on run argv
	set verb to item 1 of argv
	-- POSIX file only works outside the UTM tell block.
	set paths to {}
	repeat with a in rest of argv
		set end of paths to POSIX file (a as text)
	end repeat
	set appPath to system attribute "UTM_APP"
	if appPath is "" then set appPath to "UTM"
	with timeout of 600 seconds
		using terms from application "UTM"
		tell application appPath
			if verb is "exists" then
				return (exists virtual machine (item 2 of argv)) as text
			else if verb is "status" then
				return (status of virtual machine (item 2 of argv)) as text
			else if verb is "file" then
				return POSIX path of ((file of virtual machine (item 2 of argv)) as alias)
			else if verb is "create" then
				set vmName to item 2 of argv
				set nics to {my primaryNic(item 3 of argv, argv, 8)}
				if (item 5 of argv) is not "none" then set end of nics to {hardware:(item 5 of argv), mode:host}
				set cfg to {name:vmName, architecture:"aarch64", machine:"virt", memory:((item 6 of argv) as integer), cpu cores:0, hypervisor:true, uefi:true, icon:"windows-11", directory share mode:VirtFS, drives:{{interface:NVMe, guest size:((item 7 of argv) as integer)}, {removable:true, interface:USB}, {removable:true, interface:USB}, {removable:true, interface:USB}}, network interfaces:nics, displays:{{hardware:"virtio-ramfb-gl", dynamic resolution:true}}}
				set vm to make new virtual machine with properties {backend:qemu, configuration:(cfg & {tpm:true, rtc local time:true, sound devices:{{hardware:"intel-hda"}}})}
				export vm to item 3 of paths
				delete vm
				return vmName
			else if verb is "create-mac" then
				set vmName to item 2 of argv
				set cfg to {name:vmName, os:macOS, memory:((item 4 of argv) as integer), cpu cores:((item 5 of argv) as integer), drives:{{removable:true}, {guest size:((item 6 of argv) as integer)}}}
				if (item 3 of argv) is not "latest" then set cfg to cfg & {macRecoveryIpsw:item 2 of paths}
				set vm to make new virtual machine with properties {backend:apple, configuration:cfg}
				set cfg to configuration of vm
				set nics to network interfaces of cfg
				return address of item 1 of nics
			else if verb is "install-mac" then
				install virtual machine (item 2 of argv)
				return "installing"
			else if verb is "query-install" then
				set r to query install virtual machine (item 2 of argv)
				return "installing:" & ((installing of r) as text) & ", progress:" & ((progress of r) as text)
			else if verb is "start-provision" then
				start virtual machine (item 2 of argv) provisioning {username:(item 3 of argv), password:(item 4 of argv), full name:(item 5 of argv), automatic login:true, remote login:true}
				return "started"
			else if verb is "create-linux" then
				set vmName to item 2 of argv
				set vm to make new virtual machine with properties {backend:qemu, configuration:{name:vmName, architecture:"aarch64", machine:"virt", memory:((item 4 of argv) as integer), cpu cores:0, hypervisor:true, uefi:true, icon:"ubuntu", drives:{{interface:VirtIO, source:item 5 of paths}, {removable:true, interface:USB, source:item 6 of paths}}, network interfaces:{my primaryNic(item 3 of argv, argv, 8)}, displays:{{hardware:"virtio-gpu-gl-pci", dynamic resolution:true}}}}
				-- TSO lets FEX skip its x86 memory-ordering emulation
				set cfg to configuration of vm
				set d to drives of cfg
				set item 1 of d to {id:(id of item 1 of d), guest size:((item 5 of argv) as integer)}
				set cfg to {tso:true, sound devices:{{hardware:"intel-hda"}}} & cfg
				set drives of cfg to d
				update configuration of vm with cfg
				set cfg to configuration of vm
				return (tso of cfg) as text
			else if verb is "import" then
				set vm to import new virtual machine from item 1 of paths
				set cfg to configuration of vm
				set d to drives of cfg
				set item 2 of d to {id:(id of item 2 of d), source:item 2 of paths}
				set item 3 of d to {id:(id of item 3 of d), source:item 3 of paths}
				set drives of cfg to d
				update configuration of vm with cfg
				return name of vm
			else if verb is "start" then
				if (count of argv) > 2 then
					set envList to items 3 thru -1 of argv
					start virtual machine (item 2 of argv) environment envList
				else
					start virtual machine (item 2 of argv)
				end if
			else if verb is "start-disposable" then
				set envList to {}
				repeat with i from 3 to count of argv
					set end of envList to (item i of argv)
				end repeat
				if envList is {} then
					start virtual machine (item 2 of argv) saving false
				else
					start virtual machine (item 2 of argv) saving false environment envList
				end if
			else if verb is "stop" then
				stop virtual machine (item 2 of argv) by force
			else if verb is "kill" then
				stop virtual machine (item 2 of argv) by kill
			else if verb is "capture-screen" then
				capture screen virtual machine (item 2 of argv) to (item 2 of paths)
			else if verb is "input-text" then
				input keystroke (virtual machine (item 2 of argv)) text (item 3 of argv)
			else if verb is "input-scancodes" then
				set scList to {}
				repeat with i from 3 to count of argv
					set end of scList to ((item i of argv) as integer)
				end repeat
				input scan code (virtual machine (item 2 of argv)) codes scList
			else if verb is "input-click" then
				set pos to {(item 3 of argv) as integer, (item 4 of argv) as integer}
				set b to "left"
				if (count of argv) > 4 then set b to item 5 of argv
				if b is "right" then
					input mouse click (virtual machine (item 2 of argv)) at pos with mouse button «constant MsBtMsRt»
				else if b is "middle" then
					input mouse click (virtual machine (item 2 of argv)) at pos with mouse button «constant MsBtMsMd»
				else
					input mouse click (virtual machine (item 2 of argv)) at pos
				end if
			else if verb is "guest-tools" then
				-- UTM's own Install Windows Guest Tools (fetches the latest when
				-- needed).  A download can take longer than the other commands'
				-- 10 minutes; the limit stays under the 30 minutes utmbot waits
				-- for osascript, so a timeout is reported as one (-1712)
				with timeout of 1700 seconds
					install guest tools virtual machine (item 2 of argv)
				end timeout
			else if verb is "eject" then
				set vm to virtual machine (item 2 of argv)
				set cfg to configuration of vm
				set drives of cfg to {item 1 of (drives of cfg), {removable:true, interface:USB}}
				update configuration of vm with cfg
			else if verb is "nic" then
				set cfg to configuration of virtual machine (item 2 of argv)
				set n to item ((item 3 of argv) as integer + 1) of (network interfaces of cfg)
				return (address of n) & tab & (hardware of n) & tab & (vlan guest address of n)
			else if verb is "net-modes" then
				set modes to {}
				set cfg to configuration of virtual machine (item 2 of argv)
				repeat with n in (network interfaces of cfg)
					set end of modes to (mode of n) as text
				end repeat
				set AppleScript's text item delimiters to " "
				return modes as text
			else if verb is "net-emulated" then
				set vm to virtual machine (item 2 of argv)
				set cfg to configuration of vm
				set nics to network interfaces of cfg
				set out to {}
				set modes to {}
				repeat with i from 1 to count of nics
					set n to item i of nics
					set end of modes to (mode of n) as text
					if i is 1 then
						set fw to (port forwards of n) & {my sshForward(item 3 of argv)}
						set end of out to {index:0, mode:emulated, port forwards:fw}
					else
						set end of out to {index:(i - 1), mode:emulated}
					end if
				end repeat
				set network interfaces of cfg to out
				update configuration of vm with cfg
				set AppleScript's text item delimiters to " "
				return modes as text
			else if verb is "net-restore" then
				set vm to virtual machine (item 2 of argv)
				set cfg to configuration of vm
				set nics to network interfaces of cfg
				set out to {}
				repeat with i from 1 to count of nics
					set m to emulated
					if (count of argv) >= i + 3 then
						set t to item (i + 3) of argv
						if t is "shared" then
							set m to shared
						else if t is "host" then
							set m to host
						else if t is "bridged" then
							set m to bridged
						end if
					end if
					if i is 1 then
						set fw to {}
						repeat with f in (port forwards of item 1 of nics)
							if (host port of f) is not ((item 3 of argv) as integer) then set end of fw to (contents of f)
						end repeat
						set end of out to {index:0, mode:m, port forwards:fw}
					else
						set end of out to {index:(i - 1), mode:m}
					end if
				end repeat
				set network interfaces of cfg to out
				update configuration of vm with cfg
			else if verb is "debug-log" then
				set vm to virtual machine (item 2 of argv)
				set cfg to configuration of vm
				if (item 3 of argv) is "true" then
					set cfg to {debug log:true} & cfg
				else
					set cfg to {debug log:false} & cfg
				end if
				update configuration of vm with cfg
			else if verb is "kdnet-net" then
				set vm to virtual machine (item 2 of argv)
				set cfg to configuration of vm
				set n to network interfaces of cfg
				set item 2 of n to {index:1, mode:host, vlan guest address:(item 3 of argv), vlan dhcp end address:(item 4 of argv)}
				set network interfaces of cfg to n
				update configuration of vm with cfg
			else
				error "unknown command: " & verb
			end if
		end tell
		end using terms from
	end timeout
	return ""
end run

-- The first network interface: shared, or without vmnet (an <ssh-port>
-- argument at position i) emulated with that port forwarded to SSH.
on primaryNic(macAddress, argv, i)
	using terms from application "UTM"
		if (count of argv) >= i then
			return {hardware:"virtio-net-pci", mode:emulated, address:macAddress, port forwards:{my sshForward(item i of argv)}}
		end if
		return {hardware:"virtio-net-pci", mode:shared, address:macAddress}
	end using terms from
end primaryNic

on sshForward(hostPort)
	using terms from application "UTM"
		return {protocol:TCP, host address:"127.0.0.1", host port:(hostPort as integer), guest port:22}
	end using terms from
end sshForward
