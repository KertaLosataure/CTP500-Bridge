# CTP500 Bridge for Windows

Lets you print to a Core Innovations CTP500 ("Stone Printer") Bluetooth sticker
printer from any Windows app — Word, your browser, Photos, anything with a
Print button.

It works by running a small print server on your PC. Windows talks to it with
its own built-in IPP driver, and the bridge converts each page and sends it to
the printer over Bluetooth. Nothing needs to be installed as a driver.

The printer protocol comes from the reverse-engineering work of
[voidsshadows](https://github.com/voidsshadows/CorePrint-print-server) and
[ThirtyThreeDown](https://github.com/thirtythreedown/CTP500PrinterApp).

Note: This program was made primarily with use of Claude, and basically was created for my personal use, but since it worked well I figured I would share it for those who prefer being able to actually print to their CTP500 model like it's an actual printer that can be selected rather than having to mess around in the terminal every single time. Software is provided as-is and free to use. 
This has only been tested in Windows 11, but should work with any Windows PC that supports Python and IPP printing.
Hope someone finds some use for it! ~ Kerta

## 1. Install Python and Pillow

1. Install Python 3.9 or newer from <https://www.python.org/downloads/>.
   On the first installer screen, tick **Add python.exe to PATH**.
2. Open Command Prompt and run:

   ```
   pip install pillow
   ```

3. Put the files from this folder somewhere permanent, for example
   `C:\CTP500Bridge`.

## 2. Pair the printer and find its address

1. Turn the printer on. Close the InstaPrint app on your phone (or turn the
   phone's Bluetooth off) so the phone doesn't grab the printer.
2. In Windows, go to **Settings → Bluetooth & devices → Add device →
   Bluetooth** and pick **Stone Printer**. If it asks for a PIN, try `0000`
   or `1234`.
3. Get the printer's Bluetooth address. Either:
   - run `python ctp500_bridge.py find-mac` in the bridge folder — the line
     marked "probably your printer" is the one, or
   - double-press the printer's power button; it prints its address.

## 3. Test a print directly

In Command Prompt, in the bridge folder:

```
python ctp500_bridge.py text --mac AA:BB:CC:DD:EE:FF "Hello from my PC"
```

Use your printer's real address. It's saved to `ctp500_config.json`, so you
only have to type it once.

If a sticker comes out, the Bluetooth side is working. If the print is upside
down, add `--rotate` to every command you run (including in the .bat files).

You can also print image files straight away:

```
python ctp500_bridge.py print photo.jpg
```

## 4. Start the bridge

Double-click **start_bridge.bat**. A window opens and shows
`listening on http://127.0.0.1:631/ipp/print`. Leave it open.

You can check on it any time by opening <http://127.0.0.1:631/> in a browser.

## 5. Add it as a printer in Windows

1. **Settings → Bluetooth & devices → Printers & scanners → Add device**.
2. Wait a moment, then click **Add manually** (Windows 11) or
   **The printer that I want isn't listed** (Windows 10).
3. Choose **Select a shared printer by name** and enter:

   ```
   http://127.0.0.1:631/ipp/print
   ```

4. Click **Next**. Windows should pick the **Microsoft IPP Class Driver** by
   itself and add "CTP500 Thermal Printer".
5. Print a test page from the printer's settings page.

If option 3 doesn't work, try **Add a printer using an IP address or
hostname**, set **Device type** to **IPP Device**, and enter `127.0.0.1`.

## 6. Printing tips

- **Paper sizes.** The printer offers sticker sizes 48 mm wide (30, 50, 80,
  100, 150 and 200 mm long), plus Letter and A4. Pick a 48 mm size in the
  Print dialog for stickers at true size.
- **Documents.** Letter/A4 pages are cropped to their content and shrunk to
  the 48 mm roll, so a shopping list comes out readable rather than tiny. Use
  `--no-autocrop` if you'd rather keep the page layout exactly.
- **Photos** are dithered automatically. Use `--dither always` or
  `--dither never` to force it, and `--darkness 1.3` (darker) or
  `--darkness 0.8` (lighter) to adjust.
- **Long prints cut off?** Add `--band-height 200` so long pages are sent in
  smaller pieces.
- Blank pages are skipped, so they don't waste stickers.

## 7. Start it automatically

1. Press **Win+R**, type `shell:startup`, press Enter.
2. Right-click **start_bridge_background.bat** → **Show more options** →
   **Create shortcut**, and move the shortcut into the Startup folder.

The bridge then runs quietly whenever you sign in. Its log goes to
`bridge.log` in the bridge folder.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Could not start the server on port 631" | Something else uses port 631, or the bridge is already running. Use `--port 8631` and add the printer as `http://127.0.0.1:8631/ipp/print`. |
| Print job says "failed" | Check the window or `bridge.log`. A Bluetooth error usually means the printer is off, asleep, or connected to your phone. |
| "No Bluetooth socket support" | Make sure Python is 3.9 or newer (`python --version`). If it still fails, install Python from python.org. |
| Windows asks you to pick a driver | Your Windows version may not have the IPP class driver. Update Windows, or use the `print` and `text` commands instead. |
| Prints are faint | Add `--darkness 1.3`. |

To see what would be printed without the printer, run
`python ctp500_bridge.py serve --dry-run preview` — pages are saved as PNG
files in the `preview` folder instead.
