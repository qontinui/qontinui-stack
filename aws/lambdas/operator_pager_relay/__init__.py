# Package marker for pytest only (not shipped: the Lambda zip carries handler.py
# alone). With it, pytest puts aws/lambdas/ on sys.path instead of this
# directory, so the sibling cognito_presignup_autolink test's `import handler`
# keeps resolving to ITS handler.py rather than this one.
