# 具身智能实验资源排程服务 scaffold

This repository is an intentionally incomplete starting point for a pure backend service. It contains booking contracts, lab resource calendars, and a Docker-based scaffold validator; no requested scheduling API or allocation engine is implemented.

Business theme: 具身智能产业学院成立
Theme source: https://www.chinanews.com/scroll-news/news1.html
Required stack: Python 3.12, HTTP, JSON Schema, Docker Compose

Validate the baseline inputs with:

```sh
docker compose run --rm --no-deps scaffold-check
```

The implementation must preserve the contracts and fixtures, add the service and its automated tests, and provide a repeatable Docker-based black-box self-test. Real robots or campus systems must not be used.
