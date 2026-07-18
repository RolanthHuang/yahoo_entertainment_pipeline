# Yahoo Entertainment News Scraper

This project uses Selenium to scrape Yahoo Taiwan entertainment news, extract article content, and use an LLM to generate structured analysis results.

## Features

- Scrapes Yahoo Taiwan entertainment news list
- Opens each article and extracts the article body
- Uses an LLM to generate:
  - News summary
  - Named entities: people / groups
  - Concert-related classification
- Exports results to CSV and JSON
- Supports Docker execution

## Project Files

- `yahoo_entertainment_pipeline.py`: main scraper and LLM pipeline
- `Dockerfile`: Docker image definition
- `requirements.txt`: Python dependencies
- `.dockerignore`: Docker build ignore rules
- `.gitignore`: Git ignore rules
- `output/`: generated CSV / JSON output folder

## Build Docker Image

```bash
docker build -t yahoo-entertainment-scraper .
```

## Run with Docker

Create an output folder first:

```bash
mkdir -p output
```

Run the scraper:

```bash
docker run --rm \
  -e OPENAI_API_KEY="your_api_key_here" \
  -v "$PWD/output:/app/output" \
  yahoo-entertainment-scraper
```

The output files will be saved in the `output/` folder.

## Output Columns

The CSV output contains:

- 新聞標題
- 新聞連結
- 新聞來源
- 發布時間文字
- 推估發布時間
- 新聞內文
- 新聞內文摘要
- 實體(人名/團體)
- 是否為演唱會

## Security Notes

Do not commit your OpenAI API key to GitHub.

Use an environment variable instead:

```bash
export OPENAI_API_KEY="your_api_key_here"
```

The `.gitignore` file excludes output files and environment files by default.
