# mtop

A good-looking system monitor for your Mac. It shows CPU, GPU, memory,
temperatures, SSD, battery, network and power, plus the apps using the most
CPU or RAM.

**You do not need to download anything from this page.** Pick one version below,
copy its one line into Terminal, and you are done.

## Step 1. Pick a version

There are two versions. They are separate files, so you can try both and keep
the one you like. Neither replaces the other.

| | **Classic** | **New look** |
| --- | --- | --- |
| File | `mtop.py` | `mtop-new.py` |
| Screens | one screen, plus a ports view (`p`) | three screens: Overview, Ports & power, Core |
| Animation | only in the ports view | opening animation, moving cores, power flow |
| Tested | longer, on more Macs | newer, less tested |
| Pick it if | you want the proven one | you want the newest design |

Not sure? Start with **Classic**.

## Step 2. Open Terminal

Press `Command` + `Space`, type **Terminal**, press `Return`.

## Step 3. Copy ONE of these lines, paste it into Terminal, press `Return`

**Classic:**

```bash
curl -fsSL https://raw.githubusercontent.com/David-Nguyen210/mtop/main/mtop.py -o ~/mtop.py && python3 ~/mtop.py
```

**New look:**

```bash
curl -fsSL https://raw.githubusercontent.com/David-Nguyen210/mtop/main/mtop-new.py -o ~/mtop-new.py && python3 ~/mtop-new.py
```

## Step 4. Type your Mac password, press `Return`

Nothing appears on screen while you type the password. That is normal.

That's it. mtop is now running. Press `q` to quit.

## Next time

mtop is already on your Mac. Open Terminal and paste the line for the version you chose:

| Version | Paste this |
| --- | --- |
| Classic | `python3 ~/mtop.py` |
| New look | `python3 ~/mtop-new.py` |

## Get the latest version

mtop does not update by itself. To get new features and fixes, paste the same
line from Step 3 again. It replaces your old copy with the newest one.

## Don't want to type a password?

Add `--no-sudo` to the end:

| Version | Paste this | What you lose |
| --- | --- | --- |
| Classic | `python3 ~/mtop.py --no-sudo` | watts and clock speed |
| New look | `python3 ~/mtop-new.py --no-sudo` | only the CPU / GPU watt split and clock speed |

## Controls

| Key | Classic | New look |
| --- | --- | --- |
| `q` | quit | quit |
| `↑` `↓` | select an app | select an app |
| `k` | quit the selected app (asks first) | same |
| `s` | sort apps by CPU or RAM | same |
| `c` | clear the RAM cache | same |
| `p` | ports, power and heat view | same |
| `1` `2` `3` or `Tab` | | change screen |
| `m` | | motion: smooth, still or eco |

You can also click the buttons with the mouse, click an app, or scroll.

## What the new look adds

| Screen | What it shows |
| --- | --- |
| `1` Overview | CPU, GPU, memory, temperatures, SSD, battery, network, power and the busiest apps |
| `2` Ports & power | your Mac seen from above: what is plugged in where, watts in and out of each port, heat and fans |
| `3` Core | the chip alive: every core is a dot in orbit, and the busier it is, the faster it circles |

- Memory, and each app's CPU and RAM, are counted the way Activity Monitor counts them.
- Battery health is the figure macOS shows in System Settings.
- The whole Mac's watts are shown even without a password.
- `m` sets the motion: `smooth` (animated), `still` (one update a second) or `eco`
  (one update every 5 seconds). Animation makes the Terminal app hold about 120 MB
  more memory while it plays, so `eco` is the lightest. The new look also rests by
  itself when its window is in the background.

## Questions

**Why does it ask for my password?**
macOS only gives power readings to an administrator. mtop only reads sensors.
It never changes anything on your Mac. The whole program is the single file
`mtop.py` on this page, so anyone can read exactly what it does.

**It says "xcode-select" or asks to install developer tools.**
Click **Install** and wait for it to finish, then do Step 3 again. This
happens once on Macs that have never used Python.

**Which Macs does it work on?**
Any Mac with an Apple chip (M1 or newer). Intel Macs should work too, but are
less tested.

**What is the RAM cache, and do I need to clear it?**
When you open an app or a file, macOS keeps a copy in spare memory so it
loads faster next time. That copy is the cache. You almost never need to
clear it: macOS gives that memory back to apps the moment they ask for it.
The `c` key is there for the rare case where you want to see how much memory
is really free, for example before starting a very large job.

**Why does the cache fill up again after I clear it?**
Because macOS does it on purpose. Empty memory is wasted memory, so macOS
uses any spare RAM to keep recently used files ready. A few minutes after
clearing, it has filled up again. That is normal and healthy, not a leak.

**Does a full cache, or clearing it, affect my laptop?**
A full cache does not slow your Mac down, use more battery, or wear anything
out. Clearing it is harmless too, but it does not make your Mac faster. For a
few minutes afterwards things can even feel slightly slower, because files
have to be read from the SSD again.

**So how do I know if my Mac is short of memory?**
Ignore the cache and look at two things in the memory panel. `pressure`
should say `normal`, and `Swap` should say `inactive` or stay small. If
pressure says `warning` or `critical`, or swap keeps growing, quit some apps
from the Top apps list (press `s` to sort by RAM). mtop also tells you in the
line at the bottom when memory is tight.

**The picture looks squashed.**
Make the Terminal window bigger. It looks best at 120 x 40 or larger.

**How do I remove it?**
Classic: paste `rm ~/mtop.py` into Terminal.
New look: paste `rm -r ~/mtop-new.py ~/Library/Caches/mtop` into Terminal.
