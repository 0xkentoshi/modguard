# ModGuard README GIF UX fix

This update changes only `README.md`.

GitHub `blob/.../*.mp4` pages are repository file viewers, not a proper video player.
The README GIFs already autoplay and loop, so the cleanest portfolio UX is to make
them non-clickable and remove the misleading full-MP4 link.

Apply in your GitHub working copy:

```powershell
git add README.md
git commit -m "Improve README demo playback"
git push
```

No pytest run is required.
