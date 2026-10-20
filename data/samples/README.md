# Samples (demo / synthetic material only)
This folder holds small sample files for demos and manual tests, for example a still frame, a short
staged clip, or a printed demo ticket QR. Only add material you are allowed to use, and never add
recordings of real passengers without their consent.
- Synthetic demo records (sessions, detections, boardings, tickets, validations) are written into the
  main database by `python scripts/generate_dummy_data.py` and flagged as synthetic.
- Recorded videos for `--source video` go in `data/videos/` (see `data/videos/README.txt`).
- Demo ticket QR codes are written to `data/qr_codes/demo/` by `scripts/generate_dummy_data.py`;
  real tickets and their QR codes are issued with `python scripts/generate_qr.py`.
Everything here is demo data and must never be treated as real passenger data.
