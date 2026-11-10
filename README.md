# mtop

A good-looking system monitor for any Mac, in one Python file.

CPU, GPU, memory, temperatures, SSD, battery, network and power, plus the apps
using the most CPU or RAM and a plain-language health check.

## Run it

Paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/David-Nguyen210/mtop/main/mtop.py -o ~/mtop.py && python3 ~/mtop.py
```

It asks for your Mac password, because macOS only gives power and usage
readings to `sudo`. After the first time, just run `python3 ~/mtop.py`.

No password, or not an administrator? Basic mode shows everything except
watts and clock speed:

```bash
python3 ~/mtop.py --no-sudo
```

To see it with fake data first (any computer):

```bash
python3 ~/mtop.py --demo
```

## Controls

| Key | Action |
| --- | --- |
| `q` | quit |
| `↑` `↓` | select an app |
| `k` | quit the selected app (asks first) |
| `s` | sort apps by CPU or RAM |
| `c` | clear the RAM cache |

You can also click the buttons at the bottom, click an app row, or scroll.

## Notes

- Works on Apple Silicon Macs (M1 and later). Intel Macs are supported but less tested.
- Needs Python 3, which macOS offers to install the first time you type `python3`.
- Looks best in a window of at least 120 x 40.
- It only reads sensors. It never writes to hardware.
