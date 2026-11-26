# StockLedger

Stock with serial numbers, quotes, PDF invoices and simple UK sole-trader (cash basis) accounting, as one small Docker container for Unraid.

* Stock: serial-tracked items (laptops, routers, printers) and bulk items (cables), with cost, sale price, profit and margin per unit
* Quotes -> invoices, PDF with your logo and the serial numbers sold, payments (part or full)
* Expenses with receipt upload, other income, weekly / monthly / yearly / tax-year sales and profit
* Tax-year summary grouped by the self-employment headings, CSV exports
* Login, nightly zip backups (database + logo + receipts)

## How updates work

```
you change the code -> push to GitHub -> GitHub runs the tests and builds the image
                    -> publishes ghcr.io/<you>/stockledger:latest -> Unraid: "check for updates" -> "apply update"
```

Your data lives in appdata and the backups folder, never in the image, so updating never touches it.
Settings -> About shows the running version so you can confirm an update arrived.

## One-time setup

### 1. Put the code on GitHub
1. On github.com click **New repository**. Name it exactly `stockledger` (the image name comes from the repo name),
   choose Private or Public, and do **not** tick "add a README".
2. Upload this folder, either way:
   * **Browser:** on the new empty repo choose *uploading an existing file* and drag in everything from this folder,
     including the hidden `.github` folder (turn on "show hidden files" if you can't see it). Commit to `main`.
   * **Git:** in this folder run

         git init -b main
         git add .
         git commit -m "Initial commit"
         git remote add origin https://github.com/YOUR_GITHUB_USERNAME/stockledger.git
         git push -u origin main

3. Open the repo's **Actions** tab. The "Build and publish Docker image" run takes a couple of minutes and should end
   with a green tick. If the test step fails, nothing is published.

### 2. Let Unraid download the image
A new GitHub package starts out **private**. Choose one:

* **Public (simplest, recommended).** The image contains only the program, none of your data. On GitHub open your
  profile -> **Packages** -> `stockledger` -> **Package settings** -> change visibility to Public.
  Unraid can then pull it with no login.
* **Private.** On the Unraid terminal run `docker login ghcr.io -u YOUR_GITHUB_USERNAME` and paste a personal access
  token (classic) with only the `read:packages` permission. Unraid's login may not survive a reboot, so update
  checks can start failing until you log in again.

### 3. Add (or switch) the container on Unraid
* **New install:** copy `unraid-template.xml` to `/boot/config/plugins/dockerMan/templates-user/stockledger.xml`,
  edit it and replace `YOUR_GITHUB_USERNAME` (lower case), then Docker tab -> **Add Container** -> pick *stockledger*.
  Or add it by hand: Repository `ghcr.io/YOUR_GITHUB_USERNAME/stockledger:latest`, port `8088 -> 8080`,
  `/mnt/user/appdata/stockledger -> /data`, `/mnt/user/backups/stockledger -> /backups`,
  variables `PUID=99`, `PGID=100`, `TZ=Europe/London`.
* **Already running the locally built one:** Docker tab -> click *stockledger* -> **Edit** -> change **Repository** to
  `ghcr.io/YOUR_GITHUB_USERNAME/stockledger:latest` -> **Apply**. The paths stay the same, so your data carries over.
  Press "Back up now" in Settings first.
* **Compose Manager plugin:** use `docker-compose.yml` (edit the username).

Browse to `http://<your-server>:8088`.

## Updating
1. Change the code in the repo (edit on GitHub, push from Git, or upload the new files) on the `main` branch.
2. Wait for the green tick under the **Actions** tab.
3. On Unraid, Docker tab -> **Check for Updates**. When *stockledger* shows "update ready", click it and choose
   **apply update**. (The Community Applications "Auto Update Applications" plugin can do this on a schedule.)
4. Check Settings -> About for the new version.

Tag a release (`git tag v1.1.0 && git push --tags`) if you also want a fixed, pinnable version tag such as `:1.1.0`.
To roll back, set the container's Repository to an older tag (`ghcr.io/.../stockledger:<short-commit-id>`
or a `v` tag) and apply. Database upgrades happen automatically on start, so take a backup before big updates.

## Backups
Every night after 02:00 (change with `BACKUP_HOUR`) a zip is written to the **/backups** folder and the newest 30 are kept
(`BACKUP_KEEP`). Point `/backups` at a different share or disk from appdata, and include that share in whatever you
already back up off the server. Settings -> Backups also has "Back up now" and download links.

**Restore:** stop the container, unzip a backup, copy `stockledger.db` and the `uploads` folder into
`/mnt/user/appdata/stockledger/`, start the container.

## Everyday use
1. **Stock -> New product** once per model (e.g. "Dell Latitude 7420"). Leave "Track serial numbers" ticked for items that have them.
2. **Stock -> Add stock**: pick the product, enter what you paid, paste or scan the serial numbers (one per line).
   Add any delivery cost and the supplier receipt here. This is how purchases of goods for resale are recorded.
3. **Quotes / Invoices -> New**: pick a customer, add items. For serial items you tick exactly which units are sold
   (or scan the serial into the box). Issue the invoice, then **Download PDF** and email it yourself.
4. When the money arrives, **Record a payment** with the date received. Under the cash basis that date decides the tax year.
5. **Money -> Expenses** for everything else you spend. Don't enter goods you bought to sell there; Add stock covers those.
6. **Reports -> Tax summary** for the figures for your return; **Sales & profit** for weekly/monthly/yearly.

A USB barcode scanner works as a keyboard: click into a serial box and scan. Every scan ends with Enter, which the
serial list and search boxes understand.

## Notes and limits
* HTTP on your LAN only. If you ever expose it, put it behind a reverse proxy with HTTPS and set `COOKIE_SECURE=1`.
* Not VAT registered: there is no VAT handling. Not included: credit notes/refunds (void the invoice instead),
  mileage, use of home, capital allowances on cars, and submitting to HMRC.
* The tax summary is a bookkeeping aid, not tax advice. Check it with HMRC guidance or an accountant.
* Making Tax Digital for Income Tax is compulsory for sole traders with qualifying income over £50,000 from April 2026
  (£30,000 from April 2027, £20,000 from April 2028). This app does not submit to HMRC.

## Building locally instead (no GitHub)
`docker build -t stockledger:latest .` in this folder, then use `stockledger:latest` as the repository.
Unraid can't "check for updates" on a locally built image; rebuild and recreate the container instead.

## Settings via environment variables
`PUID`/`PGID` (99/100), `TZ`, `PORT` (8080), `DATA_DIR` (/data), `BACKUP_DIR` (/backups), `BACKUP_HOUR` (2),
`BACKUP_KEEP` (30), `COOKIE_SECURE` (unset).

## Tests
`python tests/run_all.py` (needs `pip install -r requirements.txt`). GitHub runs this before every publish.
