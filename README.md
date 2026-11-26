# StockLedger

Stock with serial numbers, quotes, PDF invoices and simple UK sole-trader (cash basis) accounting, as one small Docker container for Unraid.

* Stock: serial-tracked items (laptops, routers, printers) and bulk items (cables), with cost, sale price, profit and margin per unit
* Quotes -> invoices, PDF with your logo and the serial numbers sold, payments (part or full)
* Expenses with receipt upload, other income, weekly / monthly / yearly / tax-year sales and profit
* Tax-year summary grouped by the self-employment headings, CSV exports
* Login, nightly zip backups (database + logo + receipts)

## Install on Unraid 7.3.4

1. Copy this whole folder to your server, e.g. to `/mnt/user/appdata/stockledger-src`
   (over the network share `\\tower\appdata`, or with WinSCP/scp).
2. Open the Unraid web terminal (or SSH in) and build the image once:

       cd /mnt/user/appdata/stockledger-src
       docker build -t stockledger:latest .

3. Add the container, either way:
   * **Template:** copy `unraid-template.xml` to `/boot/config/plugins/dockerMan/templates-user/stockledger.xml`.
     Docker tab -> **Add Container** -> choose *stockledger* from the template list -> Apply.
   * **Manually:** Docker tab -> Add Container -> Repository `stockledger:latest`, add
     port `8088 -> 8080`, path `/mnt/user/appdata/stockledger -> /data`,
     path `/mnt/user/backups/stockledger -> /backups`, variables `PUID=99`, `PGID=100`, `TZ=Europe/London`.
   * **Compose Manager plugin:** use `docker-compose.yml`.
4. Browse to `http://<your-server>:8088`, create your login, then go to **Settings** to upload your logo and
   add your address and bank details.

### Backups
Every night after 02:00 (change with `BACKUP_HOUR`) a zip is written to the **/backups** folder and the newest 30 are kept
(`BACKUP_KEEP`). Point `/backups` at a different share or disk from appdata, and ideally include that share in
whatever you already back up off the server. Settings -> Backups also has a "Back up now" button and download links.

**Restore:** stop the container, unzip a backup, copy `stockledger.db` and the `uploads` folder into
`/mnt/user/appdata/stockledger/`, start the container.

### Updating
Replace the files in the source folder, run `docker build -t stockledger:latest .` again, then edit/restart the
container from the Docker tab. Your data lives in appdata, so it is untouched.

## Everyday use
1. **Stock -> New product** once per model (e.g. "Dell Latitude 7420"). Leave "Track serial numbers" ticked for items that have them.
2. **Stock -> Add stock**: pick the product, enter what you paid, paste or scan the serial numbers (one per line).
3. **Quotes / Invoices -> New**: pick a customer, add items. For serial items you tick exactly which units are sold
   (or scan the serial into the box). Issue the invoice, then **Download PDF** and email it yourself.
4. When the money arrives, **Record a payment** with the date received. Under the cash basis that date decides the tax year.
5. **Money -> Expenses** for everything else you spend. Don't enter stock purchases there; Add stock covers those.
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

## Settings via environment variables
`PUID`/`PGID` (99/100), `TZ`, `PORT` (8080), `DATA_DIR` (/data), `BACKUP_DIR` (/backups), `BACKUP_HOUR` (2),
`BACKUP_KEEP` (30), `COOKIE_SECURE` (unset).
