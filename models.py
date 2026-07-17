"""ChatMemory 领域常量。

数据库继续存字符串以兼容现有 API；字符串定义集中在这里，避免 main、分类器和查询
各自维护一份状态表。
"""

LLM_DEFAULT = ""
LLM_PENDING = "llm_pending"
LLM_SUCCESS = "llm_success"
LLM_PROACTIVE = "proactive"
LLM_ORPHAN = "orphan"

SEND_PREPARED = "prepared"
SEND_ATTEMPTED = "send_attempted"

K_TEXT = "text"
K_IMAGE = "image"
K_VIDEO = "video"
K_VOICE = "voice"
K_FILE = "file"
K_FACE = "face"
K_FORWARD = "forward"
K_SYSTEM = "system_event"

MEDIA_KINDS = {K_IMAGE, K_VIDEO, K_VOICE, K_FILE, K_FACE, K_FORWARD}

VALID_LLM_STATUSES = {
    LLM_DEFAULT,
    LLM_PENDING,
    LLM_SUCCESS,
    LLM_PROACTIVE,
    LLM_ORPHAN,
}
VALID_SEND_STATUSES = {"", SEND_PREPARED, SEND_ATTEMPTED}
