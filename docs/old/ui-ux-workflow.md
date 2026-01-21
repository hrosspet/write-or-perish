# Write or Perish – Extended UI/UX Workflow

## Overview
Write or Perish is a web‑based digital journal for archiving personal thoughts, stories, and emotions. Its dual purpose is to serve as a public archive of human expression and to collectively reach the goal of generating 1M tokens per day. All user‑generated text—including content created by users and LLM‑generated responses—is public and contributes to an archive for future AI training.

## User Onboarding & Data Policies
- **Landing Page:**  
  - Displays a short description of the app, its purpose (archiving human experience), and an introduction to its mechanics.
  - Clearly explains that all content is public and may be used for training future LLMs.
- **Privacy and Data Sharing Policy:**  
  - Before signing up, users are presented with a privacy/data sharing policy that explains:
    - Data will be public and may be used for any purpose (including AI training).
    - No HIPAA‑protected information or personal data of children under 13 (or the applicable age of digital consent) should be submitted.
- **Onboarding Intro:**  
  - After a successful signup, a short interactive introduction is shown (and is later accessible via a "help" or "info" button).  
  - The intro explains node composition and tree navigation, what gets sent to the LLM, and how token counting works (tokens are recalculated only upon creation of an LLM response).

## User Authentication
- **Twitter OAuth Login:**  
  - Users log in via Twitter OAuth.
  - (Optionally, users can set a preferred display handle, which may differ from their Twitter handle; internally, the connection is maintained but is not publicly revealed).
- **Dashboard Redirection:**  
  - Upon login, users are directed to their dashboard.

## Dashboard & Node Display
- **Dashboard Contents:**  
  - **Statistics:**  
    - Personal metrics (today’s token count and cumulative tokens).
    - Global statistics highlighting the collective goal of 1M tokens per day.
  - **Content List:**  
    - A list of the user's previously created nodes is displayed in anti‑chronological order.
    - Each node appears as a preview (showing a truncated version of long‑form content) with a small visual indicator (icon and number) representing the number of child nodes.
- **Node Detail & Navigation:**  
  - When a node preview is clicked, the node becomes “highlighted” for a detailed view.
  - In the highlighted state:
    - The selected node is shown in full and is editable.
    - All other nodes in the tree are presented only as previews.
    - The UI displays visible tree edges connecting parent and child nodes.
    - Ancestors of the highlighted node appear above, and its immediate children appear below as previews.

## Node Composition, Editing, and Linking
- **Creating Nodes:**  
  - From the dashboard, clicking a "write" button opens an empty input field.
  - When users submit text, it is stored in the database under their account and appears immediately.
  - Text is not immediately editable; users can click a three‑dot menu in the node’s top‑right to enable editing.
  - Every submission (or edit) is timestamped and stored; for the MVP, reverting to previous versions is not permitted.
- **Adding New Child Nodes:**  
  - Once a node is sent, a plus sign appears below the text field, offering two options: "Add Text" or "LLM Response."
  - **Add Text:**  
    - Clicking this option dynamically adds an input field below the parent node for additional user-authored content.
  - **LLM Response:**  
    - Clicking this option sends the thread (the highlighted node plus its parent nodes, joined chronologically) as a prompt to the OpenAI API (using the **gpt-4.5‑preview** model).
    - The resulting LLM‑generated reply is stored as a new child node.
    - The token count (including both input and output tokens) is updated in both personal and global statistics.
- **Linking Nodes:**  
  - Each text node is accessible via a unique URI so that it can be shared.
  - Users can link a pre‑existing node as a child:
    - **Link Only:**  
      - The linked node appears as a bubble or frame (with its preview nested inside a secondary bubble).
    - **Link with Additional Text:**  
      - The user can add extra text above the linked preview.
  - Linked nodes are treated as a distinct type (“link”) and displayed accordingly in the tree view.

## Feed Page – Discovering Content from All Users
- **Feed Page Access:**  
  - Accessible from the dashboard via a clearly labeled navigation button.
  - Designed to foster community engagement by displaying content created by all app users.
- **Content Display:**  
  - The Feed shows “top nodes” (nodes that represent the start of a content thread) from all users.
  - These nodes are arranged in anti‑chronological order (most recent first), allowing users to discover the latest contributions.
- **Preview Presentation:**  
  - Each node in the Feed is shown as a preview (a truncated version of the full text) along with a clear visual indicator of the number of child nodes.
  - Clicking a preview navigates users to the detailed view of that node, where the full text and its tree structure are displayed.
- **User Engagement:**  
  - The Feed page encourages users to read and interact with content from others, reinforcing the app’s archival and community‑driven ethos.
  - Public profiles and brief user descriptions accompany nodes, providing context and enabling discovery of interesting voices within the community.

## Data Management & Public Profile
- **Export & Delete Functionality:**  
  - The dashboard provides options for users to export all of their app data (e.g., as JSON or CSV) or to delete it.
  - A warning is provided indicating that deletion clears only the app’s data and does not affect data already transmitted to external services (e.g., OpenAI).
- **Public User Profile:**  
  - Each user's dashboard is public.
  - Users can add a short description (up to 128 characters) that is visible to others but not editable by them.

## Global Considerations & Statistics Updates
- **Statistics Updates:**  
  - Global and personal token statistics are updated under the hood in the following cases:
    - When a new LLM response is generated.
    - When the user navigates to the dashboard.
    - When the user manually refreshes the page.
- **Combined Views:**  
  - The dashboard integrates personal content (nodes and token counts) with global progress (the collective token count and the daily 1M target).

## Responsive & Accessible Design
- **Responsiveness:**  
  - The UI adapts smoothly to both desktop and mobile views.
- **Accessibility:**  
  - Visual cues (such as edges connecting nodes, highlighted focal nodes, and clear child count indicators) help users navigate the tree structure.
  - The design includes provisions for keyboard navigation, adequate contrast, and screen reader-friendly labels.