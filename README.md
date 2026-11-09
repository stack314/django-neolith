# django-neolith

Deploy Django apps, not Django projects.

**Status: pre-alpha. Nothing is installable yet.**

Neolith lets a Django app run inside a shared process or as its own service without changing its code. Apps talk to each other only through typed contracts, so the same code and the same tests hold in both arrangements.

This repository holds the specification work and its reference implementation.

- [Vision](VISION.md)
- [Specification](spec/README.md) (draft 0.1)
- [Roadmap to 0.1](docs/roadmap.md)
- [Guiding principles](docs/principles.md)
- [Architecture decision records](docs/adr/README.md)
- [Open questions](docs/open-questions.md)
- [Contributing](CONTRIBUTING.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

Licensed under the [BSD 3-Clause License](LICENSE).
