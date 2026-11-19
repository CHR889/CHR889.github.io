# AI Agent Space News (GitHub Pages build)

Public, serverless build of the AI Agent Space Chinese news digest.

A GitHub Actions workflow runs every 2 hours, collects the public RSS feeds
listed in `news_site/sources.json`, machine-translates titles and summaries to
Chinese, and publishes the generated static site to GitHub Pages.

- Site: https://chr889.github.io/
- RU-only page: https://chr889.github.io/runews/ (stories published on `.ru` domains)
- Generator: `news_site/build_news.py` (Python standard library only)
- Advertorial filter: a source can declare `exclude_rubrics`; the generator
  reads the article's rubric metadata and drops matching items (used for
  Rossiyskaya Gazeta's `Образ жизни` lifestyle/advertorial section).
- Workflow: `.github/workflows/deploy.yml`

Only headlines, short snippets, source names, timestamps and links back to the
original articles are published; full article text is never republished.

## Local preview

```bash
python3 news_site/build_news.py
python3 -m http.server 8000 --directory public
```

Generated output stays in `public/` and `data/`, which are git-ignored.
