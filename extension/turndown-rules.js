// Extra Turndown rules for the clipper. Injected before capture.js, which
// calls LooreTurndownRules(td); also loadable from Node for the test.
//
// Substack (and similar) wrap block content in a link:
//   <a href><div><picture><img></picture></div></a>
// Turndown puts blank lines around block content, and a blank line inside
// link text breaks the link in CommonMark ("[", the image, "](href)" on
// separate lines). Collapse every line break inside link text to a space, so
// an image link comes out as [![alt](src)](href) on one line.
var LooreTurndownRules = function (td) {
  td.addRule('linkWithBlockContent', {
    filter: function (node) {
      return node.nodeName === 'A' && !!node.getAttribute('href');
    },
    replacement: function (content, node) {
      var text = content.replace(/\s*\n\s*/g, ' ').trim();
      var href = node.getAttribute('href').replace(/([()])/g, '\\$1');
      var title = (node.getAttribute('title') || '').replace(/\s*\n\s*/g, ' ').trim();
      if (title) title = ' "' + title.replace(/"/g, '\\"') + '"';
      return '[' + text + '](' + href + title + ')';
    },
  });
  return td;
};
if (typeof module !== 'undefined') module.exports = LooreTurndownRules;
