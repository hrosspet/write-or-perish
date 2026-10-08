import Foundation

/// The Terms & Conditions, version 2.0, copied verbatim from
/// `frontend/src/components/TermsModal.js` (the backend only knows whether the
/// accepted version is current: `CURRENT_TERMS_VERSION = "2.0"`).
///
/// Inline Markdown marks what the web renders bold (`**…**`), italic (`*…*`)
/// and as links. `ios/scripts/check_terms_text.py` compares this file's text
/// with the web component; run it after either side changes.
///
/// Every string literal in `TermsText.blocks` is terms text, in reading order
/// (the check script depends on that).
enum TermsBlock: Hashable {
    case title(String)
    /// The TL;DR box: a bold heading and a numbered list.
    case summaryBox(title: String, items: [String])
    /// A quoted agreement in a box.
    case quoteBox([String])
    case sectionTitle(String)
    case subheading(String)
    case paragraph(String)
    case bullets([String])
    /// A list without bullets (the web's `listStyle: none`).
    case plainList([String])
    case table(header: [String], rows: [[String]])
    case rule
    case footnote(String)
}

enum TermsText {
    static let version = "2.0"

    static let blocks: [TermsBlock] = [
        .title("Terms & Conditions"),
        .summaryBox(title: "TL;DR — The 5 Things You Need to Know", items: [
            "**This is alpha software.** Things will break. Data could be lost. No guarantees.",
            "**Your content is private by default.** Only you can see it, and AI cannot access it, unless you explicitly change the settings.",
            "**All content is encrypted at rest,** but developers can technically decrypt it (every such event is logged). This is a trust-based model, not a cryptographic guarantee.",
            "**If you enable AI chat,** your content is sent to third-party AI providers (OpenAI, Anthropic) for processing. If you enable AI training, that's **essentially irrevocable for already-trained models.** Also, for AI training, **don't submit content you don't have the rights for!**",
            "**You own your content.** We claim no rights to it except what's needed to run the service — and, if you opt in to training, a license to train our own models too.",
        ]),
        .rule,

        .sectionTitle("1. What Loore Is"),
        .paragraph("Loore is a personal journaling app with optional AI features. It's currently in **alpha release** — meaning it is early, experimental software shared primarily among a small group of people."),
        .paragraph("By using Loore, you agree to the terms below."),

        .sectionTitle("2. Alpha Disclaimer — Please Read This"),
        .paragraph("**Loore is provided \"as is,\" with no warranties of any kind.**"),
        .bullets([
            "The app **will** contain bugs. Features **will** change or disappear without notice.",
            "**Data loss is possible.** While we back up the server daily, we make no guarantee that your data will survive any particular incident — infrastructure failure, migration error, bug, or anything else.",
            "**There is no uptime guarantee.** The service may be unavailable at any time, for any duration, without notice.",
            "There is currently **no account deletion feature.** If you need your account deleted, contact us at info@loore.org and we'll handle it manually.",
            "**No SLA, no support guarantees, no refunds.** This is alpha.",
        ]),
        .paragraph("By using Loore, you accept that you are doing so **entirely at your own risk.** The developers assume no responsibility for any data loss, damages, or other problems that may occur."),

        .sectionTitle("3. Your Content Is Yours"),
        .paragraph("You retain full ownership of everything you create on Loore."),
        .paragraph("We claim **no ownership and no license** over your content except for what is minimally necessary to operate the service: storing it, encrypting it, displaying it back to you, and processing it with AI if and only if you have opted into that."),
        .paragraph("**One exception — AI training opt-in:** If you enable the \"AI training\" setting on specific content (see Section 6), you grant both the relevant AI providers **and Loore** a license to use that content for training machine learning models. More details in Section 6."),

        .sectionTitle("4. Privacy & How Your Data Is Handled"),
        .subheading("What's Private by Default"),
        .paragraph("When you create content on Loore, it is **private by default:**"),
        .bullets([
            "**Visibility:** Only you can see it.",
            "**AI access:** No AI can read it.",
        ]),
        .paragraph("You control both of these settings per piece of content, and you can change them at any time."),
        .subheading("Privacy Controls"),
        .paragraph("**Who can see your content (visibility):**"),
        .plainList([
            "Private (default) — Only you",
            "Circles — Shared with specific groups *(coming soon — not yet functional)*",
            "Public — Anyone can see it",
        ]),
        .paragraph("**How AI can use your content:**"),
        .plainList([
            "None (default) — No AI access whatsoever",
            "Chat — AI can read this content to respond to you (not used for training — see Section 5)",
            "Train — AI providers and Loore may use this content for model training (see Section 6)",
        ]),
        .subheading("Encryption"),
        .paragraph("All user content — text, audio files, transcripts, and profiles — is **encrypted at rest** using AES-256-GCM with encryption keys managed by Google Cloud KMS (envelope encryption)."),
        .paragraph("**What this means in practice:**"),
        .bullets([
            "Your data is encrypted on disk and in the database.",
            "It must be **decrypted server-side** whenever it needs to be processed — for displaying it to you, for AI interactions, or for search.",
            "The development team has server access and **can technically decrypt your data.** We commit to not doing so except for responding to legal requirements, or with your explicit permission. Every decryption event is logged and auditable.",
            "This is a **trust-based model.** We are being transparent about what we can and cannot guarantee, rather than making cryptographic promises we can't back up.",
        ]),
        .paragraph("**What is NOT encrypted:**"),
        .bullets([
            "**Semantic embeddings** — abstract mathematical representations of your content — are stored unencrypted, isolated by your user ID. These enable search and AI features. They contain semantic information about your content but not the content itself.",
            "**Metadata** such as timestamps, content relationships, and privacy settings is not encrypted.",
        ]),
        .subheading("Who Else Touches Your Data"),
        .paragraph("Loore uses the following third-party services that may process your data:"),
        .table(header: ["Service", "What For"], rows: [
            ["**Google Cloud Platform (GCP)**", "Server hosting and encryption key management (KMS)"],
            ["**OpenAI**", "AI chat, audio transcription, text-to-speech, and (if you opt in) model training"],
            ["**Anthropic**", "AI chat"],
            ["**Email provider (SMTP)**", "Sending login magic links"],
        ]),
        .paragraph("No other third-party services have access to your user content in the deployed application."),

        .sectionTitle("5. AI Chat Mode"),
        .paragraph("When you set content to **\"Chat\"** AI usage:"),
        .bullets([
            "That content may be sent to OpenAI or Anthropic's servers so the AI can read it and respond to you.",
            "Per these providers' API terms of service, content sent through the API is **not used for training** their models.",
            "We use separate API keys for chat vs. training to enforce this separation.",
            "If you later change the setting to \"None,\" future AI interactions will no longer include that content — but we cannot recall data already processed in past chat sessions.",
        ]),

        .sectionTitle("6. AI Training Mode — Read This Carefully"),
        .paragraph("When you set content to **\"Train\"** AI usage, you are making a significant choice:"),
        .subheading("What Happens"),
        .bullets([
            "Your content will be submitted to AI providers (currently OpenAI) for potential use in **training future AI models.**",
            "**Loore** may also use this content to train its own models in the future.",
            "In exchange, OpenAI provides free daily API credits that help keep Loore running.",
        ]),
        .subheading("What You Must Understand"),
        .bullets([
            "**For models that have already been trained on your data, this is irrevocable.** You can withdraw consent going forward, and your data will not be included in future training runs — but it cannot be removed from models that have already learned from it.",
            "If you change the setting back to \"Chat\" or \"None,\" future training will stop, but past training cannot be undone.",
            "You **must have the legal rights** to any content you mark for training. If it's your original writing, you're fine. If it contains someone else's copyrighted work, song lyrics, or other material you don't have rights to — **do not enable training on it.** You bear full responsibility for this.",
        ]),
        .subheading("The License You Grant"),
        .paragraph("By enabling \"Train\" on any content, you grant:"),
        .bullets([
            "**To the relevant AI providers** (currently OpenAI): a license to use that content for development and improvement of their services, including model training, research, evaluation, and testing.",
            "**To Loore:** a license to use that content for training machine learning models, research, and development of the Loore service and related products.",
        ]),
        .paragraph("These licenses are:"),
        .bullets([
            "**Non-exclusive** — you can do whatever else you want with your content.",
            "**Royalty-free** — no payment is owed by either party.",
            "**Revocable for future use** — you can turn off training and your content will not be included in future training runs.",
            "**Irrevocable for past training** — content already used to train a model cannot be extracted from that model.",
        ]),
        .subheading("OpenAI Content Sharing Agreement"),
        .paragraph("By using Loore's AI training features, you also agree to OpenAI's Content Sharing Agreement:"),
        .quoteBox([
            "This Content Sharing Agreement is between OpenAI, L.L.C. (\"us\" or \"we\") and you (\"Customer\"). This Content Sharing Agreement is incorporated into the terms located at openai.com unless the parties have negotiated a separate agreement for the Services, in which case such agreement will govern (the \"Business Terms\"). Capitalized terms not defined here are defined in the Business Terms or the Data Processing Agreement between the parties in connection with the Services (the \"DPA\"). This Content Sharing Agreement takes precedence in the event of any conflict.",
            "Notwithstanding anything set forth in the Business Terms, we may use Customer Content to develop and improve the Services, including for training our models and other research, development, evaluation, and testing purposes (\"Development Purposes\"). You expressly agree that use of Customer Data for the Development Purposes is not subject to the provisions of the DPA. OpenAI will process Customer Data for Development Purposes as an independent Data Controller. You are responsible for all Input provided by you and your End Users.",
            "You also represent and warrant that you have the rights, licenses, and permissions necessary – including as applicable that you have provided any notice to End Users, and collected any relevant consent from End Users (\"Notice\") – to provide the Input to the Services for the Development Purposes. You agree that you and your End Users will not provide any information as Input to the Services that you or your End Users do not want to be used for Development Purposes, such as sensitive, confidential, or proprietary information. You also agree that you will not use the Services to process (a) any data that includes or constitutes \"Protected Health Information,\" as defined under the HIPAA Privacy Rule (45 C.F.R. Section 160.103), or (b) any Personal Data of children under 13 or the applicable age of digital consent. You also agree that you will provide OpenAI a copy of your Notice upon OpenAI's request.",
        ]),
        .paragraph("The full agreement is incorporated into and governed by OpenAI's Business Terms at [openai.com](https://openai.com)."),

        .sectionTitle("7. What You Agree Not to Do"),
        .bullets([
            "Do not submit **Protected Health Information** (as defined under HIPAA) or **personal data of children** under 13 or the applicable age of digital consent.",
            "Do not mark content for AI training unless you hold the necessary rights.",
            "Do not use Loore for any illegal purpose.",
            "Do not attempt to access other users' private content.",
        ]),

        .sectionTitle("8. Liability"),
        .paragraph("**To the maximum extent permitted by applicable law:**"),
        .bullets([
            "Loore and its developers are not liable for any damages arising from your use of the service — including but not limited to data loss, service interruptions, security incidents, or any consequences of content being used for AI training.",
            "This is alpha software. See Section 2.",
        ]),

        .sectionTitle("9. Changes to These Terms"),
        .paragraph("We may update these terms as Loore evolves. When we make significant changes, we will notify you (through the app or by email) and ask you to re-accept. Continued use after notification constitutes acceptance."),

        .sectionTitle("10. Contact"),
        .paragraph("For any questions, concerns, or account-related requests: [info@loore.org](mailto:info@loore.org)"),

        .rule,
        .footnote("*Terms Version: 2.0 — Last updated: February 9, 2026*"),
        .paragraph("By clicking \"I Agree\", you confirm that you have read and understand these terms and that you consent to the terms described above."),
    ]
}
