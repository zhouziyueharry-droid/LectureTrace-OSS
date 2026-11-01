# Contributing

Thanks for helping improve LectureTrace. Issues, documentation and pull requests are welcome in English or Chinese. Harry (@zhouziyueharry-droid) is the founder and primary maintainer and reviews changes for the official repository.

## Before you start

For a larger feature, open an issue with the problem, a concrete example and the proposed scope. Small, focused fixes can go straight to a PR. Check existing issues first. Include only material you have permission to share.

## Development workflow

1. Fork the repository and clone your fork. Invited collaborators may clone the shared repository directly and push their feature branches there.
2. Create a descriptive feature branch from `main`.
3. Install using the README and make a focused change.
4. Run relevant tests and the frontend build; describe results and remaining limits in the PR.
5. Open a PR against `main` and respond to review. Collaborator access is not required.

```sh
uv run --directory backend --no-sync pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Backend tests use temporary data and mock external models. Optional media dependencies may be required for some tests; CI installs FFmpeg. Do not introduce real API calls into tests. Use a separate data directory for manual experiments.

For UI changes, include screenshots made with synthetic or openly licensed content. For behavior changes, add a regression test that exercises the behavior rather than duplicating implementation details. Keep interface and data-format changes documented in the same PR.

## Attribution

Use your own Git author identity and an email associated with your GitHub account (GitHub's `noreply` address is fine). Preserve authorship when merging. Use `Co-authored-by` only for actual joint work. Describe your role in the PR so the contribution is understandable beyond its line count.

Contributions are accepted under the repository's MIT License. You retain ownership of your contribution; submitting it does not transfer your copyright to the maintainer. Submit only work you are authorized to license. Identify third-party code and its license. AI-assisted contributions are welcome when you review, understand and validate the result and describe material assistance where relevant.

## What belongs outside the repository

Keep API keys, auth files, `.env`, databases, model weights, course packages, recordings, lecture slides, student information and private screenshots out of commits and issue attachments. Use original synthetic examples for reproduction. Report security issues through the process in [SECURITY.md](SECURITY.md).

Be respectful in discussion. Focus criticism on the code and behavior, provide reproducible evidence, and avoid personal attacks, harassment or sharing another person's private information.
