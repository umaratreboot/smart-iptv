# Smart IPTV M3U — Android + Google TV

Free software, no always-on PC. **GitHub Actions runs once daily**. The generated playlist is a remote `.m3u` URL that can be loaded in compatible Android/Google TV players. GitHub checks are not guarantees of playback in your country.

## Roman Urdu setup — shuru se aakhir tak

1. [github.com](https://github.com) par login karo. **New repository** banao, name `smart-iptv`, **Public** select karo, `Create repository` dabao. Public repo mein private URLs, passwords, ya tokens KABHI na daalo.
2. Is ZIP ko extract karo. GitHub repository mein **Add file → Upload files** se `updater.py`, `config.json`, `custom_entries.m3u`, `README.md`, `state.json`, aur `output` folder upload karo. **Hidden `.github/workflows/update.yml` file ka upload ensure karo.** Web upload mein hidden folder miss ho jaye to GitHub par `Add file → Create new file` se filename `.github/workflows/update.yml` likh kar ZIP wali file ka content paste karke commit karo.
3. Repo mein **Settings → Actions → General → Workflow permissions** par `Read and write permissions` select karo (agar option available ho). `update.yml` mein bhi `contents: write` diya hua hai.
4. **Actions → Daily IPTV Update → Run workflow → Run workflow** dabao. Run green hone ke baad `output/MyIPTV.m3u` aur `output/guide.xml` files generate hongi.
5. Apna GitHub username replace karke permanent playlist URL banao:
   `https://raw.githubusercontent.com/YOUR-USERNAME/smart-iptv/main/output/MyIPTV.m3u`
   EPG URL:
   `https://raw.githubusercontent.com/YOUR-USERNAME/smart-iptv/main/output/guide.xml`
   Agar default branch `master` hai to URL mein `main` ki jagah `master` karo.
6. Android aur Google TV par VLC ya compatible IPTV player install karo. **Add playlist / M3U URL** mein playlist URL paste karo. Agar player XMLTV guide URL mangay to EPG URL do. Player ki auto-refresh setting enable karo. Google TV par URL type karne ke bajaye phone se remote keyboard use kar sakte ho.
7. **Roz 07:17 Pakistan time ke aas-paas** workflow trigger hoga (GitHub scheduled jobs delay ya kabhi skip kar sakta hai). Trigger UTC 02:17 hai. Manual Run workflow anytime use karo.

## Kya automatically hota hai

- Configured publicly-listed Live TV playlists se nayi entries roz import hoti hain; URL duplicates nikalte hain.
- Stream HTTP response ki basic probing hoti hai. Previously working URL ko 3 consecutive **checks in which it is present** fail hone par output se remove karte hain. First-time failing URLs initial output mein nahi aate. Jab koi stream wapas accessible ho to woh dobara add ho sakti hai.
- Source temporarily unavailable ho to last successful output ko preserve karne ki koshish hoti hai.
- EPG: configured XMLTV source ya upstream M3U header mein listed XMLTV URL se daily download attempt hota hai. Sirf `tvg-id` se match hone wale programmes retain hote hain. Agar koi valid matching guide nahi milti to `guide.xml` empty ho sakti hai ya purana successful guide retain hoga. **EPG availability guaranteed nahi.**
- Media files host/download nahi hoti; M3U sirf URLs index karti hai.

## Movies / Series kaise add karein

`custom_entries.m3u` edit karo. Sirf apne media ya clearly licensed/free direct stream links add karo:

```m3u
#EXTINF:-1 group-title="Movies | Personal",My Free Licensed Movie
https://your-authorized-host.example/path/movie.mp4
#EXTINF:-1 group-title="Series | My Show",My Show S01E01
https://your-authorized-host.example/path/episode01.mp4
```

Ye **example URLs** hain, working media nahi. Website/Netflix/YouTube watch URL aksar direct stream nahi hota. File edit ke baad Actions run karo. Series har episode ko ek entry ki tarah show karti hai; true Netflix-style seasons nahi.

## EPG configuration

`config.json` mein `"epg_urls": ["https://AUTHORIZED-EPG-PROVIDER.example/guide.xml.gz"]` set karo jab tumhare paas valid, authorized XMLTV URL ho. Ya `epg_auto_discover: true` rakho jo upstream playlist `x-tvg-url` field ko check karta hai. URLs must be actual XMLTV XML or gzip files; guide IDs need to match M3U `tvg-id` exactly. Agar available na ho to blank guide normal hai. Multiple XMLTV URLs provided hon to script **pehla matching source** use karta hai, full merge nahi.

## Source settings

`config.json`: `playlist_sources` ko apni sources se badlo. `max_entries` per-source candidate limit hai; upstream order par depend karta hai. Default Pakistan 65, India 65, English 70, total max ~200. Countries list official permissive licensing ka proof nahi; aapki zimmedari hai source ke redistribution permissions aur location restrictions check karna. Never add private subscription credentials to public repo.

## Troubleshooting

- **Run failed at push**: Repo Settings → Actions → General → Workflow permissions mein write permissions verify karo; protected branch rules may block the bot.
- **Playlist khaali**: GitHub runner se sab first-time streams unreachable ho sakti hain. `Actions` log dekho aur legal sources/custom links add karo. Geo restrictions Pakistan aur GitHub runner mein different ho sakte hain.
- **Channel runs in GitHub but not Android**: Some stream URLs need headers, tokens, or geo access; this simple cross-player format does not inject special headers.
- **EPG blank**: Live channel `tvg-id` must match XMLTV channel id; source is missing, oversized, or unavailable.
- **Repo inactivity**: GitHub public repos mein 60 days no activity par schedules automatically disabled ho sakte hain. Re-enable workflow when needed.
- **False positives**: Basic HTTP probe is not a full audio/video decoder and cannot ensure playback. A 403 may be geoblocking rather than permanent death.

## Rights + privacy

Free/public listing does not automatically prove authorized redistribution. Verify permissions for every stream before use. For movies/series use your own content, openly licensed videos, or authorized direct URLs. Private links/accounts should never be exposed in a public GitHub repository. Software is an indexer, not a bypass for DRM, subscriptions, or geo-blocking.
