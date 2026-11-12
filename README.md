# mtop

A good-looking system monitor for your Mac. It shows CPU, GPU, memory,
temperatures, SSD, battery, network and power, plus the apps using the most
CPU or RAM.

## How to use it

**You do not need to download anything from this page.** Just follow these 3 steps.

### Step 1. Open Terminal

Press `Command` + `Space`, type **Terminal**, press `Return`.

### Step 2. Copy this line, paste it into Terminal, press `Return`

```bash
curl -fsSL https://raw.githubusercontent.com/David-Nguyen210/mtop/main/mtop.py -o ~/mtop.py && python3 ~/mtop.py
```

### Step 3. Type your Mac password, press `Return`

Nothing appears on screen while you type the password. That is normal.

That's it. mtop is now running. Press `q` to quit.

## Next time

mtop is already on your Mac. Open Terminal and paste:

```bash
python3 ~/mtop.py
```

## Get the latest version

mtop does not update by itself. To get new features and fixes, paste the line
from Step 2 again. It replaces your old copy with the newest one:

```bash
curl -fsSL https://raw.githubusercontent.com/David-Nguyen210/mtop/main/mtop.py -o ~/mtop.py && python3 ~/mtop.py
```

## Don't want to type a password?

Use basic mode. It shows everything except watts and clock speed:

```bash
python3 ~/mtop.py --no-sudo
```

## Two looks: pick the one you like

**Classic** is everything above. It stays exactly as it is.

**New look** is a redesign you can try next to it. It has three screens you switch
with `1`, `2`, `3` or `Tab`:

| Screen | What it shows |
| --- | --- |
| `1` Overview | CPU, GPU, memory, temperatures, SSD, battery, network, power and the busiest apps |
| `2` Ports & power | your Mac seen from above: what is plugged in where, watts in and out of each port, heat and fans |
| `3` Core | the chip alive: every core is a dot in orbit, and the busier it is, the faster it circles |

It also counts memory and each app's CPU and RAM the same way Activity Monitor does,
shows battery health as the figure macOS shows in System Settings, and shows the whole
Mac's watts even without a password.

To try the new look, paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/David-Nguyen210/mtop/main/mtop-new.py -o ~/mtop-new.py && python3 ~/mtop-new.py
```

Next time: `python3 ~/mtop-new.py`. To go back to classic: `python3 ~/mtop.py`. Both can
stay on your Mac side by side.

Extra keys in the new look: `1` `2` `3` or `Tab` change screen, and `m` sets the motion
to `smooth` (animated), `still` (one update a second) or `eco` (one update every 5
seconds). Animation makes the Terminal app hold about 120 MB more memory while it
plays, so `eco` is the lightest; the new look also rests by itself when its window is
in the background. To remove it: `rm -r ~/mtop-new.py ~/Library/Caches/mtop`.

## Controls

| Key | What it does |
| --- | --- |
| `q` | quit |
| `↑` `↓` | select an app |
| `k` | quit the selected app (asks first) |
| `s` | sort apps by CPU or RAM |
| `c` | clear the RAM cache |
| `p` | ports, power and heat view (press `p` again to go back) |

You can also click the buttons at the bottom with the mouse, click an app, or scroll.

## Ports, power and heat view

Press `p` to see a picture of your Mac with its ports:

- which ports have something plugged in, and what it is (drive, display, charger)
- the watts coming in from the charger, and the watts going out of each USB-C port
- for a drive: its size, free space and read / write speed
- whether each port is Thunderbolt 3, 4 or 5
- where the heat leaves the Mac, and how fast the fans spin

Port details are best on Macs with an Apple chip. If your Mac does not report
something, mtop leaves it out instead of guessing.

## Questions

**Why does it ask for my password?**
macOS only gives power readings to an administrator. mtop only reads sensors.
It never changes anything on your Mac. The whole program is the single file
`mtop.py` on this page, so anyone can read exactly what it does.

**It says "xcode-select" or asks to install developer tools.**
Click **Install** and wait for it to finish, then do Step 2 again. This
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
Paste `rm ~/mtop.py` into Terminal.
