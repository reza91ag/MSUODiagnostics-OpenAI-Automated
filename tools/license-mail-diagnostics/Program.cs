using Google.Cloud.Firestore;
using System.Collections.Generic;
var db=FirestoreDb.Create("msuo-community-licensemanager");
try {
  var snap=await db.Collection("CommunityEdition").Limit(200).GetSnapshotAsync();
  var counts=new Dictionary<string,int>();
  var maxAttempts=0L;
  foreach(var doc in snap.Documents) {
    var state=doc.TryGetValue<string>("deliveryStatus", out var s)?s:"missing";
    counts[state]=counts.GetValueOrDefault(state)+1;
    var attempts=doc.TryGetValue<long>("deliveryAttempts",out var n)?n:0;
    maxAttempts=Math.Max(maxAttempts,attempts);
  }
  Console.WriteLine("CommunityEdition sampled docs: "+snap.Count);
  foreach(var entry in counts)Console.WriteLine("deliveryStatus "+entry.Key+": "+entry.Value);
  Console.WriteLine("Maximum delivery attempts: "+maxAttempts);
} catch(Exception ex) {Console.WriteLine("Firestore read ERROR "+ex.GetType().Name+": "+ex.Message); Environment.ExitCode=1;}
