# AI Agent Space News (GitHub Pages build)

Public, serverless build of the AI Agent Space Chinese news digest.

A GitHub Actions workflow runs every 2 hours, collects the public RSS feeds
listed in `news_site/sources.json`, machine-translates titles and summaries to
Chinese, and publishes the generated static site to GitHub Pages.

- Site: https://chr889.github.io/
- Generator: `news_site/build_news.py` (Python standard library only)
- Workflow: `.github/workflows/deploy.yml`

Only headlines, short snippets, source names, timestamps and links back to the
original articles are published; full article text is never republished.

## Local preview

```bash
python3 news_site/build_news.py
python3 -m http.server 8000 --directory public
```

Generated output stays in `public/` and `data/`, which are git-ignored.
