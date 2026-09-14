# Release 0.2.0 — verified September 14, 2026 UTC

The scoped portfolio release is complete: merged source, passing CI, published ARM image, actual AWS query/recovery verification, and both demo instances stopped.

- [Merged PR #1](https://github.com/ezhilan03/fintech-rag/pull/1)
- [Final infrastructure/source CI](https://github.com/ezhilan03/fintech-rag/actions/runs/34804520850): 84 tests, real retrieval, non-root offline container and Terraform validation passed.
- [Published image and AWS verification](https://github.com/ezhilan03/fintech-rag/actions/runs/34804436930): success.
- [Raw cloud report](../eval/evidence/2026-09-14-aws-demo.json)

The deployed image was built from `5fc8cc431b02f671c872963c4f404bc1d4ecba81`, digest `sha256:b482cbf2b955e69cc7ca07bd04e602bac551405d54393ef4fec6d1bd12792ec4`. Commit `166b835` subsequently made the SSM document explicitly start Docker after bootstrap; runtime source, dependencies, Dockerfile and demo data are identical to the deployed image. The cloud run used SSM document version 2 containing this startup safeguard. Subsequent changes attach evidence and documentation only.

## Live results

- Six synthetic documents, six versions, six chunks; all six unchanged-source replays made no additional chunks.
- Actual API health 200; unsupported R99 abstained.
- Actual Haiku comparison returned exact excerpts from Alpha and Beta, with two source citations and version/content hashes. One observed request took 1,846 ms; this is a smoke-test observation, not a latency benchmark or SLA.
- PostgreSQL backup uploaded to encrypted, versioned S3; restored into a disposable database. Document/version/chunk counts matched and chunk-content checksums matched.
- Database stopped deliberately: API health 503. Database restarted: API health returned to 200.
- CloudWatch log stream received application events. `Portfolio/FintechRAG / DemoSuccess` recorded 1. No paging notification is claimed.
- Workflow cleanup stopped the RAG host. Administrator verification confirmed both RAG and Recon instances stopped.
- Dedicated RAG security group has no inbound rules and allows outbound HTTPS only. The one-hour stop timer was verified active.

## Cost boundary

RAG retains a 12 GB gp3 disk; Recon retains 16 GB. At the previously checked us-east-1 $0.08/GB-month rate, combined disks are about $2.24/month. The first RAG image is 444 MB compressed. Light on-demand compute, image storage, S3 and seven-day logs fit the intended under-$5 AWS target, but actual usage, taxes and pricing determine the bill. Anthropic calls are billed separately. The account-wide $5 budget has warning notifications and does not impose a hard cap.

The service is a private, single-host synthetic demonstration. It is not an always-on public service, high-availability system, or authoritative financial guidance. The fixed retrieval/grounding cases and recovery drill support specific engineering claims; they do not establish general answer accuracy or enterprise readiness.
