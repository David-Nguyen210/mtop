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

## Don't want to type a password?

Use basic mode. It shows everything except watts and clock speed:

```bash
python3 ~/mtop.py --no-sudo
```

## Controls

| Key | What it does |
| --- | --- |
| `q` | quit |
| `↑` `↓` | select an app |
| `k` | quit the selected app (asks first) |
| `s` | sort apps by CPU or RAM |
| `c` | clear the RAM cache |

You can also click the buttons at the bottom with the mouse, click an app, or scroll.

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

**The picture looks squashed.**
Make the Terminal window bigger. It looks best at 120 x 40 or larger.

**How do I remove it?**
Paste `rm ~/mtop.py` into Terminal.
