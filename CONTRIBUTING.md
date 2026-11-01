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

## NTU AI6130 group collaboration

This section is for members of Harry's **NTU AI6130 — Large Language Models (26S1)** assignment group. For this group assignment, we are building additional features and improvements on the existing LectureTrace project.

LectureTrace is a long-term personal project founded and maintained by Harry. During the AI6130 group project, group members may be invited as collaborators to make working together easier; Harry continues to coordinate the project's direction, review and ongoing maintenance.

After the AI6130 group project ends, collaborator (direct write) access granted for that collaboration will normally be removed as the project returns to its ongoing maintenance arrangement. There is no expectation that course participants continue maintaining the project. Anyone interested in staying involved is warmly encouraged to contact Harry to discuss a continuing role and appropriate access. Contributions through forks and pull requests remain welcome at any time.

Contributor credit and the associated commit, PR and review records will be preserved when course access ends. Contributors are welcome to link to these public records from their profiles, portfolios or CVs, with an accurate description of the work they contributed. Thank you for the time, care and ideas you bring to the project.

### NTU AI6130 小组同学须知

本节专门写给一起完成 **NTU AI6130 — Large Language Models（26S1）小组作业**的组员。本次小组作业将在既有的 LectureTrace 项目基础上，一起进行新功能和改进的增量开发。

LectureTrace 是由 Harry 发起并计划长期维护的个人项目。AI6130 小组项目期间，可根据协作需要为组员开放 Collaborator 权限，方便大家在同一仓库的功能分支上开发；项目方向、代码审核及后续维护由 Harry 统筹。

本次 AI6130 小组项目结束后，项目将回归日常维护安排，课程协作期间授予的 Collaborator（直接写入）权限将默认收回。同学无需承担课程结束后的持续维护义务；如果有兴趣长期参与，非常欢迎联系 Harry，一起商量后续参与方式和相应权限。无论是否保留直接写入权限，都欢迎继续通过 fork 和 PR 贡献。

大家的贡献署名及相应的 commit、PR 和 review 记录会得到保留。欢迎在个人主页、作品集或简历中引用这些公开记录，并准确说明自己负责的工作。感谢每位同学为项目投入的时间、心力和想法。

## Attribution

Use your own Git author identity and an email associated with your GitHub account (GitHub's `noreply` address is fine). Preserve authorship when merging. Use `Co-authored-by` only for actual joint work. Describe your role in the PR so the contribution is understandable beyond its line count.

Contributions are accepted under the repository's MIT License. You retain ownership of your contribution; submitting it does not transfer your copyright to the maintainer. Submit only work you are authorized to license. Identify third-party code and its license. AI-assisted contributions are welcome when you review, understand and validate the result and describe material assistance where relevant.

## What belongs outside the repository

Keep API keys, auth files, `.env`, databases, model weights, course packages, recordings, lecture slides, student information and private screenshots out of commits and issue attachments. Use original synthetic examples for reproduction. Report security issues through the process in [SECURITY.md](SECURITY.md).

Be respectful in discussion. Focus criticism on the code and behavior, provide reproducible evidence, and avoid personal attacks, harassment or sharing another person's private information.
