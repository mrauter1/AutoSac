# Intent: Attachment visibility + any-file uploads

## User-reported problems
1. Uploaded attachments are not visible on ticket pages.
2. The system should support uploads for any document/file type, not only PNG/JPEG.

## Required outcomes
- Ensure uploaded attachments reliably appear in the ticket thread as attachment links.
- Allow any file type to be uploaded on requester ticket create and requester replies.
- Preserve existing upload size/count limits and authorization checks.
- Keep image metadata extraction for images when possible, but do not reject non-image files.

## Constraints
- No regression in ticket creation/reply flows.
- Attachment download access control must remain unchanged.
- Update UI copy to reflect generalized files instead of image-only language.

## Validation goals
- Requester can upload a non-image file and see an attachment link in ticket detail.
- Requester can still upload image files.
- Attachment links render and download endpoint works with stored mime type.
