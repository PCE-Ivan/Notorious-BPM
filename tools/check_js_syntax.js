// Syntax-checks JavaScript files with the JavaScriptCore engine that ships
// with macOS (no node needed). Used by test_frontend_syntax.py:
//
//     osascript -l JavaScript tools/check_js_syntax.js static/app.js ...
//
// Prints one "ok <path>" / "FAIL <path>: <error>" line per file.
ObjC.import('Foundation');

function run(argv) {
  return argv.map(function (path) {
    var src = $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js;
    try {
      new Function(src);  // parses without running; classic scripts are fine as a function body
      return 'ok   ' + path;
    } catch (e) {
      return 'FAIL ' + path + ': ' + e.name + ': ' + e.message;
    }
  }).join('\n');
}
