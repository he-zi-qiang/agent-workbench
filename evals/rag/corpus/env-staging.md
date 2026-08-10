# Environment: staging

Staging mirrors production's topology at one quarter the resources and shares
none of its stores. A staging run may never write to a production cluster, and
the DSNs are held in different secret scopes so a copied config fails closed.

The corpus in staging is a fixture, not a copy of customer documents.
