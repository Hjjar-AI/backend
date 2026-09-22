# backend/apps/master_exams/services/master_exam_service/constants.py

DELETE_MODE_DELETE = 'delete_drafts'
DELETE_MODE_KEEP = 'keep_drafts'
DELETE_MODE_PUBLISH = 'publish_drafts'

VALID_DELETE_MODES = {DELETE_MODE_DELETE, DELETE_MODE_KEEP, DELETE_MODE_PUBLISH}