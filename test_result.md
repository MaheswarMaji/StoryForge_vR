#====================================================================================================
# START - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================

# THIS SECTION CONTAINS CRITICAL TESTING INSTRUCTIONS FOR BOTH AGENTS
# BOTH MAIN_AGENT AND TESTING_AGENT MUST PRESERVE THIS ENTIRE BLOCK

# Communication Protocol:
# If the `testing_agent` is available, main agent should delegate all testing tasks to it.
#
# You have access to a file called `test_result.md`. This file contains the complete testing state
# and history, and is the primary means of communication between main and the testing agent.
#
# Main and testing agents must follow this exact format to maintain testing data. 
# The testing data must be entered in yaml format Below is the data structure:
# 
## user_problem_statement: {problem_statement}
## backend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.py"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## frontend:
##   - task: "Task name"
##     implemented: true
##     working: true  # or false or "NA"
##     file: "file_path.js"
##     stuck_count: 0
##     priority: "high"  # or "medium" or "low"
##     needs_retesting: false
##     status_history:
##         -working: true  # or false or "NA"
##         -agent: "main"  # or "testing" or "user"
##         -comment: "Detailed comment about status"
##
## metadata:
##   created_by: "main_agent"
##   version: "1.0"
##   test_sequence: 0
##   run_ui: false
##
## test_plan:
##   current_focus:
##     - "Task name 1"
##     - "Task name 2"
##   stuck_tasks:
##     - "Task name with persistent issues"
##   test_all: false
##   test_priority: "high_first"  # or "sequential" or "stuck_first"
##
## agent_communication:
##     -agent: "main"  # or "testing" or "user"
##     -message: "Communication message between agents"

# Protocol Guidelines for Main agent
#
# 1. Update Test Result File Before Testing:
#    - Main agent must always update the `test_result.md` file before calling the testing agent
#    - Add implementation details to the status_history
#    - Set `needs_retesting` to true for tasks that need testing
#    - Update the `test_plan` section to guide testing priorities
#    - Add a message to `agent_communication` explaining what you've done
#
# 2. Incorporate User Feedback:
#    - When a user provides feedback that something is or isn't working, add this information to the relevant task's status_history
#    - Update the working status based on user feedback
#    - If a user reports an issue with a task that was marked as working, increment the stuck_count
#    - Whenever user reports issue in the app, if we have testing agent and task_result.md file so find the appropriate task for that and append in status_history of that task to contain the user concern and problem as well 
#
# 3. Track Stuck Tasks:
#    - Monitor which tasks have high stuck_count values or where you are fixing same issue again and again, analyze that when you read task_result.md
#    - For persistent issues, use websearch tool to find solutions
#    - Pay special attention to tasks in the stuck_tasks list
#    - When you fix an issue with a stuck task, don't reset the stuck_count until the testing agent confirms it's working
#
# 4. Provide Context to Testing Agent:
#    - When calling the testing agent, provide clear instructions about:
#      - Which tasks need testing (reference the test_plan)
#      - Any authentication details or configuration needed
#      - Specific test scenarios to focus on
#      - Any known issues or edge cases to verify
#
# 5. Call the testing agent with specific instructions referring to test_result.md
#
# IMPORTANT: Main agent must ALWAYS update test_result.md BEFORE calling the testing agent, as it relies on this file to understand what to test next.

#====================================================================================================
# END - Testing Protocol - DO NOT EDIT OR REMOVE THIS SECTION
#====================================================================================================



#====================================================================================================
# Testing Data - Main Agent and testing sub agent both should log testing data below this section
#====================================================================================================

user_problem_statement: |
  StoryForge video factory — two-phase fix:
  Phase 1: Local LLM routing (Ollama-only for all text, strict no-cloud-fallback)
  Phase 2: Studio-StoryForge coordination (character contamination fix, per-scene cast isolation)
  Plus: True multi-user per-account key vault isolation

backend:
  - task: "Local Ollama LLM routing — 4-model task dispatch"
    implemented: true
    working: true
    file: "services/llm.py"
    stuck_count: 0
    priority: high
    needs_retesting: false
    status_history:
      - working: true
        agent: main
        comment: |
          OLLAMA_BASE_URL=http://127.0.0.1:11434. Strict local-only: when URL is set
          cloud APIs (Gemini/OpenAI/HF) are NEVER called for text. 4-model routing:
          ask_json(fast=True)->llama3.1:8b, ask_json()->qwen2.5:72b,
          ask_json(reasoning=True)->deepseek-r1:32b, ask_json(vision=True)->qwen3-vl:32b-instruct.
          parse_json() strips deepseek <think> blocks.

  - task: "agents.py — per-task model routing"
    implemented: true
    working: true
    file: "services/agents.py"
    stuck_count: 0
    priority: high
    needs_retesting: false
    status_history:
      - working: true
        agent: main
        comment: |
          write_script/identify_stories/regenerate_chunk -> ask_json (qwen2.5:72b).
          editor_pass/run_qa/apply_review_edits/improvement_pass -> ask_json_reasoning (deepseek-r1:32b).
          make_metadata -> ask_json_fast (llama3.1:8b).

  - task: "Per-account key vault — true multi-user isolation"
    implemented: true
    working: true
    file: "services/keys.py, server.py, job_queue.py, routes.py"
    stuck_count: 0
    priority: high
    needs_retesting: false
    status_history:
      - working: true
        agent: main
        comment: |
          services/keys.py: ContextVar-based per-request/per-job isolation.
          When user context IS active, PROVIDER_KEYS come ONLY from that user's vault (db.api_keys).
          No env bleed between accounts. Empty vault = empty result for provider keys.
          No context (system jobs) = env fallback for fresh-install compat.
          server.py: caller_keys() FastAPI dependency loads vault per request.
          job_queue.py: _execute_job loads owner's vault before running the handler.
          routes.py: GET/PUT/DELETE /settings/api-keys are now strictly per-user.

  - task: "Studio character table parsing"
    implemented: true
    working: true
    file: "services/studio_local.py"
    stuck_count: 0
    priority: high
    needs_retesting: false
    status_history:
      - working: true
        agent: main
        comment: |
          characters_from() now handles 3 formats: (1) story.characters list,
          (2) colon-delimited lines, (3) pipe/tab table (Dhruv-style production briefs).
          _parse_table_characters() handles ID|Name|Appearance|Restrictions format.
          Root cause of character contamination: table-format sheet was silently
          failing, causing characters_from() to raise, and the whole Studio run
          to fail before images were generated.

  - task: "Studio per-scene cast isolation — fix character contamination"
    implemented: true
    working: true
    file: "services/studio_local.py, script_parser.py"
    stuck_count: 0
    priority: high
    needs_retesting: false
    status_history:
      - working: true
        agent: main
        comment: |
          build_request() now uses KEY PRESENCE (not truthiness) to distinguish:
            'cast' key absent   -> no declaration -> _mentions() fallback (old behaviour)
            'cast' = []         -> explicit empty -> environment-only scene (allowed, no error)
            'cast' = ['dhruv']  -> explicit list  -> strictly matched to roster
          script_parser.py: CAST_RE parses **Visible cast:** lines.
          _extract_cast_ids() returns [] for environment-only, list for explicit,
          None for undeclared. chunk['cast'] stored ONLY when explicitly declared.
          BIBLE_START_RE expanded: 'Character consistency sheet', 'Shared visual style'.
          SCRIPT_START_RE expanded: 'Script, dialogue and visuals'.

metadata:
  created_by: main_agent
  version: "1.0"
  test_sequence: 1
  run_ui: false

test_plan:
  current_focus:
    - "Local Ollama LLM routing"
    - "Per-account key vault isolation"
    - "Studio character table parsing + cast isolation"
  stuck_tasks: []
  test_all: false
  test_priority: high_first

agent_communication:
  - agent: main
    message: |
      Phase 1 (LLM) and Phase 2 (Studio) complete. All 5 backend unit checks pass.
      Backend running on 8001. Ollama routing is local-only when OLLAMA_BASE_URL set.
      Per-user key vault fully isolated via ContextVar. Studio cast contamination fixed.
      Next: pipeline order (script→voice→image→compile with incremental retry),
      voice generation fix (kokoro disk issue), and create-from-script LLM segmentation.