// "Start from your tweets" (#662): the answer to the card's question
// (PrefillConsentCard), which the person can change in Settings both
// ways. The iPhone app has the same words (AccountModel.tweetSeedHelp,
// PrefillConsentCard.notedText): change both together.
//
// What a no does (backend utils/privacy): no pre-fill runs for the
// account, and every AI read of the person's writing as a whole leaves
// the tweets a pre-fill brought in out. What was already made stays, and
// the profile and intentions stay in use (replies, Glean) until edited;
// neither can be deleted.

// The handle the tweets come from: the one Loore pre-filled from, else
// the X account the person signs in with, else the username (the card's
// wording before #662).
export function tweetSeedHandle(user) {
  return (user && (user.prefilled_handle || user.twitter_handle || user.username)) || "";
}

// Where the question applies: the card asks X sign-ins; an account Loore
// pre-filled holds tweets; an answer, once given, can always be changed.
export function showsTweetSeed(user) {
  return Boolean(user && (user.twitter_login || user.prefilled_handle || user.prefill_consent));
}

export function tweetSeedHelp(user) {
  const text =
    `With Yes, Loore brings in your public tweets as @${tweetSeedHandle(user)}, ` +
    "keeps them private in your Loore, and drafts your profile and intentions " +
    "from them. With No, it brings in no more of them and leaves the ones it " +
    "brought in out whenever it reads through your writing. What it already " +
    "made stays: those tweets, which you can delete, and your profile and " +
    "intentions, which Loore keeps using in replies and Glean until you edit them.";
  return user && user.prefill_consent ? text : `${text} Until you answer, Loore may still bring them in.`;
}

// The card's line after an answer. A no on an account Loore already
// pre-filled does not leave a blank page: what was made stays.
export function tweetSeedNoted(user, answer) {
  if (answer === "yes") {
    return "Noted. We'll seed your Loore from your public tweets and have first drafts of your artifacts — like your profile and intentions — waiting when you come in.";
  }
  if (user && user.prefilled_handle) {
    return "Noted. Loore brings in no more of your tweets and leaves the ones it brought in out whenever it reads through your writing. Your profile and intentions stay, and Loore keeps using them until you edit them. You can change this in Settings.";
  }
  return "Noted. You'll start from a blank page — you can always seed from your tweets later, from Settings.";
}
