#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Runs `block` and returns the Objective-C exception it raised, or nil.
///
/// AVFAudio raises an exception, rather than returning an error, when a tap or
/// the engine meets an input format it cannot use. Swift cannot catch it, so the
/// app aborted mid-recording (#423 walk test, 2026-10-07). Swift calls this
/// through `ObjCException.catching`.
NSException * _Nullable LooreCatchException(NS_NOESCAPE void (^block)(void));

NS_ASSUME_NONNULL_END
