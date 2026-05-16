# Scan → Graded Card Lifecycle

```
[POST /v1/scans]
  --> ScanJob.status = queued
  --> response: 4 presigned PUT urls

[client uploads images directly to S3]
  --> status remains queued / transitions to uploading

[POST /v1/scans/{id}/complete]
  --> status = processing
  --> arq enqueue: process_scan({job_id, user_id})

[worker]
  --> grading_service.grade_from_images()
  --> fingerprint_service.fingerprint_from_images()
  --> insert GradedCard + Fingerprint
  --> status = complete
  --> publish ScanProgressEvent on loupe:scans:user:{user_id}

[WS /ws/scans?token=...]
  --> client receives: hello, scan_progress{status=processing, progress=0.1}
                       scan_progress{status=processing, progress=0.6}
                       scan_progress{status=complete,   progress=1.0, result=...}
```

Failures move the job to `failed` and publish a terminal event with
`message` populated.  Soft-deleted graded cards keep their fingerprint
row so the duplicate detector still sees them.
