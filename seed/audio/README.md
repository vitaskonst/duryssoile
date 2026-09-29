# Seed audio

The pronunciation clips go here. They are not in git (~611 MB). Only the
one-time import reads them (see "Seed data" in the top-level README):

```
seed/audio/
├── parasite/                 186 clips
└── commonly-mispronounced/   22 008 clips
```

Each file is named after the `filename` field of its word in
`seed/data/parasite.json` or `seed/data/commonly-mispronounced.json`.
